"""
common/services/etiquette_palette_service.py
============================================
Service domaine : étiquettes palette logistique avec code-barres GS1-128.

Source des données :
  La dernière opération sync (table ``sync_operations``) contient déjà tout ce
  dont on a besoin (marque, GTIN colis, lot, DDM). On évite ainsi un nouvel
  aller-retour EasyBeer et on s'aligne sur ce que la page Paramètres
  > Étiquettes affiche déjà.

Flow opérateur (UI à 3 sélecteurs cascadés) :
  1. Marque : NIKO / SYMBIOSE
  2. Type de bouteille : 33cl / 75cl SAFT / 75cl Eau gazeuse
  3. Goût : Gingembre, Mangue Passion, Original, …
  → on retrouve l'EAN colis, le lot et la DDM automatiquement.

Construction du code-barres GS1-128 (avec FNC1 — généré par BWIPP/treepoem) :

    (02)<GTIN-14> (15)<YYMMDD> (10)<lot> (37)<count>

L'AI 02 désigne le GTIN des **articles contenus** dans la palette (= les
caisses), conformément à l'usage logistique pour étiquette palette. L'ordre
choisi place les AI à longueur fixe (02, 15) avant les variables (10, 37),
avec FNC1 inséré automatiquement par treepoem entre les AI variables.

Le module est sans NiceGUI : utilisable depuis CLI / cron / tests.
"""
from __future__ import annotations

import datetime as _dt
import functools
import json
import logging
import re
import warnings
from dataclasses import dataclass

from common.easybeer import (
    EasyBeerError,
    get_all_products,
    get_code_barre_matrice,
)
from common.easybeer.products import determine_brand_from_label
from common.ramasse import clean_product_label, extract_gout, get_palette_layout, parse_barcode_matrix
from db.conn import run_sql

_log = logging.getLogger("ferment.services.etiquette_palette")

# ─── Garde-fou décompression d'image ────────────────────────────────────────
# Borne le nombre de pixels qu'une image peut atteindre après décompression
# pour éviter qu'un PNG de 12 MB explose en plusieurs Go en RAM côté serveur
# ("decompression bomb"). 25 Mpx = large pour iPhone 14 (12 Mpx natif) ou
# iPad Pro (idem).
_MAX_IMAGE_PIXELS = 25_000_000
try:
    from PIL import Image as _PILImage
    _PILImage.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
except ImportError:
    pass


# ─── Constantes typage ───────────────────────────────────────────────────────

BOTTLE_33 = "33cl"
BOTTLE_75_SAFT = "75cl SAFT"
BOTTLE_75_EAU_GAZ = "75cl Eau gazeuse"

BOTTLE_TYPES = (BOTTLE_33, BOTTLE_75_SAFT, BOTTLE_75_EAU_GAZ)

BRAND_NIKO = "NIKO"
BRAND_SYMBIOSE = "SYMBIOSE"

# AI 37 = "Count of trade items", longueur variable (max 8 digits).
# On ne padde pas : un "150" reste "150", treepoem gère la séparation FNC1.
_LOT_MAX_LEN = 20            # contrainte GS1 sur AI 10
_LOT_ALLOWED_RE = re.compile(r"[^A-Z0-9\-./]")


# ─── Modèles typés ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LabelEntry:
    """Un produit prêt à étiqueter, issu de la dernière sync."""
    marque: str               # "NIKO" | "SYMBIOSE"
    bottle_type: str          # "33cl" | "75cl SAFT" | "75cl Eau gazeuse"
    gout: str                 # ex: "Gingembre", "Mangue Passion", "Original"
    designation: str          # ex: "Kéfir Gingembre — 12x33cl"
    fmt: str                  # ex: "12x33", "6x33", "6x75", "4x75"
    pcb: int                  # ex: 12, 6, 4
    ean_colis: str            # GTIN colis (carton) — 13 ou 14 digits
    ean_uvc: str              # GTIN bouteille (peut être "" si non disponible)
    code_interne: str         # ex: "SK-KDF-33-GIN"
    lot_str: str              # ex: "08052027" (= DDMMYYYY de la DDM, depuis la sync)
    ddm_date: _dt.date        # date DDM
    product_label: str        # libellé produit nettoyé (ex: "Kéfir Gingembre")


@dataclass(frozen=True)
class Gs1Payload:
    """Payload GS1-128 prêt pour l'encodage par treepoem.

    ``data_with_parens`` est passé tel quel à ``treepoem.generate_barcode``
    (BWIPP convertit les ``(NN)`` en FNC1 + AI selon la spec GS1).
    """
    data_with_parens: str     # ex: "(02)03760381620415(15)260812(10)L6104(37)150"
    hri: str                  # version lisible humainement (espacée pour l'œil)


@dataclass(frozen=True)
class HistoryEntry:
    """Une étiquette générée précédemment, prête pour réimpression."""
    id: int
    ean: str                    # GTIN colis (carton)
    lot: str
    ddm: _dt.date
    fmt: str
    marque: str
    designation: str
    gout: str
    case_count: int
    full_pallet: bool
    n_copies: int
    pcb: int
    gtin_uvc: str
    code_interne: str
    bio: bool
    user_email: str
    generated_at: _dt.datetime
    sscc: str = ""              # SSCC 18 digits (vide pour entrées pré-SSCC)
    voided_at: _dt.datetime | None = None
    voided_reason: str = ""
    # archived_at : NULL = active, sinon date d'archivage. Réversible.
    # Exclue des compteurs ("imprimées aujourd'hui" etc.) tant qu'archivée.
    # Différent de voided_at (qui est lié au SSCC dans sscc_log).
    archived_at: _dt.datetime | None = None


@dataclass(frozen=True)
class SyncStatus:
    """Métadonnées sur la dernière sync étiquettes pour affichage UI."""
    has_sync: bool
    age_hours: float | None       # âge en heures depuis la dernière sync
    status: str | None            # 'applied' | 'pending' | 'fetched' | None
    product_count: int            # nb de produits dans la dernière sync


# ─── Calcul du nombre de caisses ────────────────────────────────────────────

def compute_case_count(
    fmt: str,
    *,
    full_pallet: bool,
    layers_full: int = 0,
    extras_top: int = 0,
    product_label: str = "",
) -> int:
    """Calcule le nombre total de caisses sur la palette.

    Si ``full_pallet`` est vrai : ``layout["total"] + extras_top``. Le mode
    « palette pleine » accepte des caisses en surplus (cas entrepôt :
    palette pleine + quelques caisses sur le dessus).

    Sinon : ``layers_full × per_layer + extras_top``.

    Validations :
      - ``0 ≤ layers_full ≤ layers_max``
      - ``0 ≤ extras_top < per_layer`` (un étage complet → on incrémente layers_full)

    Note : on n'impose plus ``total ≤ layout["total"]`` car les opérateurs
    chargent parfois la palette en surcharge (caisses additionnelles sur le
    dessus d'une palette pleine, ou la dernière caisse d'une commande).
    L'UI affiche un avertissement « surcharge » côté client mais accepte
    la saisie.

    Raises:
        ValueError: format inconnu, ou ``extras_top``/``layers_full`` hors bornes.
    """
    layout = get_palette_layout(fmt, product_label)
    if layout["total"] <= 0:
        raise ValueError(f"Format de palette inconnu : {fmt!r}")

    layers_max = layout["layers"]
    per_layer = layout["per_layer"]

    if not (0 <= extras_top < per_layer):
        raise ValueError(
            f"extras_top doit être entre 0 et {per_layer - 1} pour le format {fmt!r} "
            f"(un étage complet → cocher palette pleine ou ajouter un étage)",
        )

    if full_pallet:
        # Palette pleine = capacité nominale + d'éventuelles caisses sur le
        # dessus (cas surcharge). extras_top reste borné par per_layer-1.
        return layout["total"] + extras_top

    if not (0 <= layers_full <= layers_max):
        raise ValueError(
            f"layers_full doit être entre 0 et {layers_max} pour le format {fmt!r}",
        )
    return layers_full * per_layer + extras_top


# ─── Construction du payload GS1-128 ────────────────────────────────────────

def _ean_to_gtin14(ean: str) -> str:
    """Préfixe un EAN-13 avec '0' pour obtenir un GTIN-14 (logistic indicator)."""
    digits = re.sub(r"\D+", "", ean or "")
    if len(digits) == 14:
        return digits
    if len(digits) == 13:
        return "0" + digits
    raise ValueError(f"EAN/GTIN invalide (attendu 13 ou 14 digits) : {ean!r}")


def _normalize_lot(lot: str) -> str:
    """Normalise un lot pour AI 10 : majuscules, ASCII restreint, longueur ≤ 20."""
    s = (lot or "").strip().upper()
    s = _LOT_ALLOWED_RE.sub("", s)
    if not s:
        raise ValueError("Lot vide après normalisation")
    if len(s) > _LOT_MAX_LEN:
        s = s[:_LOT_MAX_LEN]
    return s


def build_gs1_128_payload(
    ean13: str,
    lot: str,
    ddm: _dt.date,
    count: int,
) -> Gs1Payload:
    """Construit la chaîne GS1-128 au format avec parenthèses pour treepoem.

    Ordre des AI (aligné sur l'usage logistique standard) :
      - 02 (GTIN-14 des articles **contenus** dans la palette, 14 digits)
      - 15 (DDM YYMMDD, 6 digits)
      - 10 (lot/batch, variable jusqu'à 20 caractères)
      - 37 (count, variable jusqu'à 8 digits)

    treepoem (BWIPP) insère automatiquement les FNC1 entre AI variables et
    le FNC1 de tête qui marque le code comme GS1-128.
    """
    if count <= 0:
        raise ValueError("count doit être > 0")
    if count > 99_999_999:
        raise ValueError("count > 99 999 999 (limite AI 37)")

    gtin14 = _ean_to_gtin14(ean13)
    yymmdd = ddm.strftime("%y%m%d")
    lot_norm = _normalize_lot(lot)

    # Format passé tel quel à treepoem : il convertit les (NN) en AI + FNC1
    data_with_parens = f"(02){gtin14}(15){yymmdd}(10){lot_norm}(37){count}"
    hri = f"(02){gtin14}  (15){yymmdd}  (10){lot_norm}  (37){count}"
    return Gs1Payload(data_with_parens=data_with_parens, hri=hri)


# ─── Classification depuis le payload sync ──────────────────────────────────

def classify_bottle_type(
    designation: str,
    marque: str,
    pcb: int | float,
    fmt: str = "",
) -> str | None:
    """Classifie un produit en 33cl / 75cl SAFT / 75cl Eau gazeuse.

    Cherche le volume dans la ``designation`` ou dans ``fmt`` (ex: "6x33").
    Règles :
      - 33cl si volume = 33
      - 75cl SAFT si NIKO ou si Symbiose en PCB=4
      - 75cl Eau gazeuse si Symbiose en PCB=6
    """
    desig = (designation or "").lower()
    fmt_low = (fmt or "").lower()
    pcb_int = int(pcb or 0)
    marque_up = (marque or "").upper()

    has_33 = (
        "33cl" in desig or "33 cl" in desig or "x33" in fmt_low or fmt_low.endswith("33")
    )
    has_75 = (
        "75cl" in desig or "75 cl" in desig or "x75" in fmt_low or fmt_low.endswith("75")
    )

    if has_33:
        return BOTTLE_33
    if has_75:
        if marque_up == BRAND_NIKO:
            return BOTTLE_75_SAFT
        if pcb_int == 4:
            return BOTTLE_75_SAFT
        if pcb_int == 6:
            return BOTTLE_75_EAU_GAZ
    return None


def extract_label_gout(designation: str, marque: str, product_label: str = "") -> str:
    """Extrait le goût (sans préfixe marque/produit) depuis la désignation.

    Ex:
      "Kéfir Gingembre — 12x33cl"                     → "Gingembre"
      "NIKO - Kéfir de fruits Gingembre — 12x33cl"    → "Gingembre"
      "Infusion probiotique Zest d'agrumes — 6x33cl"  → "Zest d'agrumes"
    """
    # Préfère le product_label nettoyé si dispo (déjà sans suffixe degré)
    base = product_label or designation
    # Couper sur '—' (séparateur format)
    if "—" in base:
        base = base.split("—", 1)[0].strip()
    elif "–" in base:
        base = base.split("–", 1)[0].strip()

    # Retirer le préfixe NIKO si présent
    base = re.sub(r"^\s*NIKO\s*[-:]\s*", "", base, flags=re.IGNORECASE).strip()

    base = clean_product_label(base)
    return extract_gout(base) or base


def _format_lot_str(lot_raw) -> str:
    """Formate un lot depuis le payload (int/float/str) en str.

    Le payload sync stocke le lot comme float (ex: 8052027.0). On retourne
    un string sans la décimale, et padding à 8 digits pour les dates DDMMYYYY
    (ex: 8052027 → "08052027").
    """
    if lot_raw is None or lot_raw == "":
        return ""
    try:
        n = int(float(lot_raw))
    except (TypeError, ValueError):
        return str(lot_raw)
    s = str(n)
    if len(s) == 7:
        # DDMMYYYY avec un seul digit pour le jour → pad
        s = "0" + s
    return s


def _parse_ddm_iso(ddm_raw) -> _dt.date | None:
    if not ddm_raw:
        return None
    try:
        return _dt.date.fromisoformat(str(ddm_raw)[:10])
    except (ValueError, TypeError):
        return None


# ─── Chargement depuis sync_operations ───────────────────────────────────────

def load_label_data_from_sync(tenant_id: str) -> tuple[list[LabelEntry], str | None]:
    """Charge les produits étiquetables depuis la dernière sync du tenant.

    Stratégie : on cherche d'abord la dernière sync ``applied``. Si aucune,
    fallback sur la dernière ``pending`` ou ``fetched`` (car le payload est
    déjà construit dès la création).

    Returns:
        (liste_de_LabelEntry, message_d_info).
        Le message d'info est ``None`` si tout va bien, sinon une chaîne à
        afficher en bandeau (ex: "Aucune sync — lance d'abord la sync").
    """
    rows = run_sql(
        """SELECT id, payload, status, applied_at, created_at
           FROM sync_operations
           WHERE tenant_id = :t AND status IN ('applied', 'pending', 'fetched')
           ORDER BY (status = 'applied') DESC, created_at DESC
           LIMIT 1""",
        {"t": tenant_id},
    )
    if not rows:
        return [], (
            "Aucune sync étiquettes disponible. Va dans "
            "Paramètres → Étiquettes et clique sur « Lancer la sync maintenant »."
        )

    op = rows[0]
    payload = op.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return [], "Payload sync illisible (JSON invalide)."

    if not isinstance(payload, list) or not payload:
        return [], "La dernière sync est vide. Relance la sync étiquettes."

    entries: list[LabelEntry] = []
    skipped = 0
    for p in payload:
        designation = (p.get("designation") or "").strip()
        marque = (p.get("marque") or "").upper().strip()
        pcb_raw = p.get("pcb") or 0
        try:
            pcb = int(float(pcb_raw))
        except (TypeError, ValueError):
            pcb = 0

        ean_colis = re.sub(r"\D+", "", str(p.get("gtin_colis") or ""))
        ean_uvc = re.sub(r"\D+", "", str(p.get("gtin_uvc") or ""))
        if not (designation and marque and pcb and ean_colis):
            skipped += 1
            continue

        bottle_type = classify_bottle_type(designation, marque, pcb)
        if bottle_type is None:
            skipped += 1
            continue

        if "—" in designation:
            product_label = clean_product_label(designation.split("—")[0])
        else:
            product_label = clean_product_label(designation)
        gout = extract_label_gout(designation, marque, product_label)

        # Format ex: "12x33", "6x75"
        # Volume depuis bottle_type (33 ou 75), PCB depuis le payload
        vol_cl = "33" if bottle_type == BOTTLE_33 else "75"
        fmt = f"{pcb}x{vol_cl}"

        ddm_date = _parse_ddm_iso(p.get("ddm"))
        if ddm_date is None:
            skipped += 1
            continue

        lot_str = _format_lot_str(p.get("lot"))
        if not lot_str:
            # Fallback : DDM au format DDMMYYYY (cohérent avec collector)
            lot_str = ddm_date.strftime("%d%m%Y")

        entries.append(LabelEntry(
            marque=marque,
            bottle_type=bottle_type,
            gout=gout,
            designation=designation,
            fmt=fmt,
            pcb=pcb,
            ean_colis=ean_colis,
            ean_uvc=ean_uvc,
            code_interne=(p.get("code_interne") or "").strip(),
            lot_str=lot_str,
            ddm_date=ddm_date,
            product_label=product_label,
        ))

    if skipped:
        _log.info("load_label_data_from_sync : %d entrées ignorées (champs manquants)", skipped)

    msg: str | None = None
    if op.get("status") != "applied":
        msg = (
            "La dernière sync n'a pas encore été appliquée par l'agent — "
            "les données affichées peuvent être obsolètes."
        )

    return entries, msg


def _load_image_map() -> list[tuple[str, str]]:
    """Charge le mapping ``assets/image_map.csv`` une fois en mémoire.

    Retourne une liste de tuples ``(canonical_lowercase, filename)``,
    triée pour être déterministe. Cache module-level via ``lru_cache``.
    """
    try:
        from pathlib import Path

        import pandas as pd
        repo = Path(__file__).resolve().parent.parent.parent
        csv_path = repo / "assets" / "image_map.csv"
        if not csv_path.exists():
            return []
        df = pd.read_csv(csv_path, encoding="utf-8")
    except Exception:
        _log.debug("Erreur chargement image_map.csv", exc_info=True)
        return []

    out: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        canonical = str(row.get("canonical", "")).strip().lower()
        filename = str(row.get("filename", "")).strip()
        if canonical and filename and (repo / "assets" / filename).exists():
            out.append((canonical, filename))
    return out


# Cache module-level : le CSV ne change qu'à un déploiement, donc on le
# charge une fois pour toute la durée de vie du process. Évite N lectures
# disque + parsing pandas à chaque rendu de la card récap et des entrées
# d'historique.
_get_image_map_cached = functools.lru_cache(maxsize=1)(_load_image_map)


def get_product_image_url(gout: str | None) -> str | None:
    """Retourne l'URL absolue de l'image produit pour un goût donné.

    Lit le mapping ``assets/image_map.csv`` (canonical → filename), avec
    cache module-level (lru_cache). Retourne ``None`` si pas de mapping.
    L'URL retournée est servie via ``app.add_static_files('/assets', ...)``.
    """
    if not gout:
        return None
    target = gout.strip().lower()
    if not target:
        return None
    for canonical, filename in _get_image_map_cached():
        if canonical == target or target in canonical or canonical in target:
            return f"/assets/{filename}"
    return None


def parse_gs1_string(text: str) -> dict[str, str]:
    """Parse une chaîne GS1-128 au format avec parenthèses.

    Ex: ``"(01)03770014427250(15)270511(10)110527"``
    →   ``{"01": "03770014427250", "15": "270511", "10": "110527"}``

    Sans parenthèses (GS1-128 brut avec FNC1), seule l'extraction du GTIN
    via AI 01 (longueur fixe) est tentée — les AI à longueur variable sans
    FNC1 sont ambigus. Si pas de parenthèses ni de FNC1, retourne {}.
    """
    out: dict[str, str] = {}
    if not text:
        return out
    # Format avec parenthèses (treepoem / human readable)
    pattern = re.compile(r"\((\d{2,4})\)([^(]*)")
    matches = pattern.findall(text)
    if matches:
        for ai, val in matches:
            out[ai] = val.strip()
        return out
    # Fallback : essayer d'extraire AI 01 si la chaîne commence par "01" + 14 digits
    m = re.match(r"^01(\d{14})", text)
    if m:
        out["01"] = m.group(1)
    return out


def parse_gs1_digits(data: str) -> dict[str, str]:
    """Parse un payload GS1-128 en pure-digits (sans parenthèses, sans FNC1).

    Utile quand l'opérateur copie-colle ou tape la chaîne complète du
    code-barres sans formatage. Le parser sait que :
      - AI 00, 01, 02 → longueur fixe (18, 14, 14)
      - AI 11, 15, 17 → longueur fixe (6 = YYMMDD)
      - AI 10, 21, 30, 37 → longueur variable, terminée par FNC1 ou fin
        de chaîne (ici on prend tout ce qui reste — convient quand l'AI
        variable est la dernière).

    Returns:
        ``{"01": "23770014427049", "15": "270508", "10": "080527"}``
        Si la chaîne ne commence pas par un AI connu, retourne ``{}``.
    """
    digits = re.sub(r"\D+", "", data or "")
    if not digits:
        return {}
    # AI fixed-length (length AFTER the 2-digit AI prefix)
    fixed = {
        "00": 18, "01": 14, "02": 14,
        "11": 6, "13": 6, "15": 6, "17": 6,
    }
    variable = {"10", "21", "30", "37", "240", "241"}
    out: dict[str, str] = {}
    i = 0
    while i < len(digits):
        if i + 2 > len(digits):
            break
        ai = digits[i:i + 2]
        i += 2
        if ai in fixed:
            length = fixed[ai]
            if i + length > len(digits):
                break
            out[ai] = digits[i:i + length]
            i += length
        elif ai in variable:
            # Pas de FNC1 dans une chaîne pure-digit → on prend le reste.
            # Convient si l'AI variable est la dernière (cas le plus fréquent).
            out[ai] = digits[i:]
            i = len(digits)
        else:
            # AI inconnu → abandon (ne pas deviner)
            break
    return out


def parse_gs1_ddm(yymmdd: str) -> _dt.date | None:
    """Parse une date GS1 au format YYMMDD → date Python.

    Convention GS1 : YY 00-49 → 20YY, YY 50-99 → 19YY (rolling century window).
    En pratique, pour des produits récents, on est dans 20YY.
    """
    if not yymmdd or len(yymmdd) != 6 or not yymmdd.isdigit():
        return None
    yy = int(yymmdd[0:2])
    mm = int(yymmdd[2:4])
    dd = int(yymmdd[4:6])
    year = 2000 + yy if yy < 50 else 1900 + yy
    try:
        return _dt.date(year, mm, dd)
    except ValueError:
        return None


# Tables de référence GS1 pour parser un GS1-128 brut (sans parenthèses).
# Utilisées par `parse_gs1_raw` (input typique : iOS AVFoundation).
_GS1_FIXED_LEN_AI = {
    "00": 18, "01": 14, "02": 14,
    "11": 6, "13": 6, "15": 6, "17": 6,
    "20": 2,
}
# AI à longueur variable (terminés par FNC1 ou fin de chaîne). Liste non
# exhaustive — on couvre ceux qu'on rencontre sur les étiquettes cartons.
_GS1_VARIABLE_AIS = {"10", "21", "22", "30", "37", "240", "241", "250", "251", "253", "254"}
# FNC1 = ASCII 29 (Group Separator). iOS AVFoundation l'inclut dans la chaîne
# décodée pour séparer les AIs variables.
_GS1_FNC1 = "\x1d"


def parse_gs1_raw(data: str) -> dict[str, str]:
    """Parse un GS1-128 dans son format brut (sans parenthèses).

    Gère le format produit par iOS AVFoundation après décodage d'un Code 128 :
    - préfixe AIM optionnel ``"]C1"`` (symbology identifier)
    - séparateur FNC1 (ASCII 29) entre AIs variables
    - valeurs alphanumériques pour les AIs variables (lot, batch…)

    Args:
        data: chaîne brute, ex: ``"]C1010377001442725015270511\\x1d10TESTLOT01"``
              ou ``"010377001442725015270511\\x1d10TESTLOT01"``

    Returns:
        ``{"01": "...", "15": "...", "10": "..."}`` — dict des AIs trouvés.
        Retourne ``{}`` si la chaîne ne contient pas d'AI reconnaissable.
    """
    s = data or ""
    if not s:
        return {}
    # Strip AIM Code Identifier si présent (iOS peut le préfixer)
    if s.startswith("]C1"):
        s = s[3:]

    out: dict[str, str] = {}
    i = 0
    n = len(s)
    while i < n:
        # Identifier l'AI : essai 2 chars, puis 3, puis 4
        ai = None
        for length in (2, 3, 4):
            if i + length > n:
                break
            candidate = s[i:i + length]
            if candidate in _GS1_FIXED_LEN_AI or candidate in _GS1_VARIABLE_AIS:
                ai = candidate
                i += length
                break
        if ai is None:
            break  # AI inconnu → on s'arrête sans deviner

        if ai in _GS1_FIXED_LEN_AI:
            length = _GS1_FIXED_LEN_AI[ai]
            if i + length > n:
                break
            out[ai] = s[i:i + length]
            i += length
            # FNC1 optionnel après AI fixe (tolérance — certains encodeurs en
            # ajoutent toujours, même si la spec ne l'exige pas).
            if i < n and s[i] == _GS1_FNC1:
                i += 1
        else:
            # AI variable : lire jusqu'à FNC1 ou fin de chaîne
            j = s.find(_GS1_FNC1, i)
            if j == -1:
                out[ai] = s[i:]
                i = n
            else:
                out[ai] = s[i:j]
                i = j + 1  # skip FNC1

    return out


def parse_gs1_to_entry(text: str) -> dict[str, str] | None:
    """Parse une string GS1-128 (parenthèses ou brut iOS) en entrée
    ergonomique pour l'API mobile.

    Utilisé par l'endpoint ``POST /api/v1/decode-gs1`` : l'app iOS décode le
    code-barres en natif (AVFoundation) puis envoie la string brute au backend.

    Args:
        text: chaîne brute scannée — formats supportés :
            - ``"(01)037...(15)270511(10)110527"`` (avec parenthèses, ex: treepoem)
            - ``"]C1010377...152705111\\x1d10TESTLOT01"`` (iOS AVFoundation)
            - ``"010377...15270511 10110527"`` (digits, fallback ancien)

    Returns:
        ``{"ean": ..., "lot": ..., "ddm": "YYYY-MM-DD" | ""}`` si AI 01 trouvé.
        ``None`` si aucun EAN lisible.
    """
    text = (text or "").strip()
    if not text:
        return None

    # Format 1 : avec parenthèses (treepoem, copier-coller humain).
    ais = parse_gs1_string(text) if "(" in text else {}
    # Format 2 : format brut iOS (AIM + FNC1 + alphanumérique).
    if not ais.get("01"):
        ais = parse_gs1_raw(text)
    # Format 3 : ancien fallback "pure digits" (utile si lot purement numérique).
    if not ais.get("01"):
        ais = parse_gs1_digits(text)
    ean = ais.get("01")
    if not ean:
        return None

    ddm_str = ais.get("15") or ais.get("17") or ""
    ddm_date = parse_gs1_ddm(ddm_str)
    return {
        "ean": ean,
        "lot": ais.get("10") or "",
        "ddm": ddm_date.isoformat() if ddm_date else "",
    }


def extract_gs1_data_from_image(image_bytes: bytes) -> dict[str, str | _dt.date] | None:
    """Décode et parse un GS1-128 depuis une image.

    Returns:
        ``{"ean": <14 digits>, "lot": <str>, "ddm": <date>}`` si on a au moins
        AI 01 (les autres sont optionnels). ``None`` si rien décodé ou pas
        d'AI 01 lisible.
    """
    try:
        import io as _io

        import zxingcpp
        from PIL import Image
    except ImportError as exc:
        _log.error("zxing-cpp ou Pillow indisponible : %s", exc)
        return None

    try:
        with warnings.catch_warnings():
            # Convertir le DecompressionBombWarning de PIL en exception pour
            # rejeter les images qui exploseraient en RAM (zip-bomb PNG).
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(_io.BytesIO(image_bytes))
            img.load()  # force le décodage pour déclencher le check Pillow
            if img.mode not in ("L", "RGB"):
                img = img.convert("RGB")
            results = zxingcpp.read_barcodes(img)
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        _log.warning(
            "Decompression bomb détectée — image rejetée (limite %d Mpx)",
            _MAX_IMAGE_PIXELS // 1_000_000,
        )
        return None
    except Exception:
        _log.exception("Erreur décodage image")
        return None

    if not results:
        return None

    # Cherche d'abord un GS1-128 complet (AI 01 + AI 15 + AI 10) — c'est ce
    # qu'imprime le Domino sur les cartons. Sinon on fallback sur le premier
    # code lisible. Évite de matcher accidentellement un sticker EAN-13 client
    # collé à côté de l'étiquette logistique.
    best: dict | None = None
    fallback: dict | None = None
    for r in results:
        text = (r.text or "").strip()
        if not text:
            continue
        ais = parse_gs1_string(text)
        ean = ais.get("01")
        if ean:
            ddm_str = ais.get("15") or ais.get("17") or ""
            ddm_date = parse_gs1_ddm(ddm_str)
            entry = {
                "ean": ean,
                "lot": ais.get("10", ""),
                "ddm": ddm_date,
            }
            # GS1-128 complet (au moins AI 01 + AI 15) → on préfère
            if ais.get("15"):
                return entry
            best = best or entry
        else:
            digits = re.sub(r"\D+", "", text)
            if 12 <= len(digits) <= 14 and fallback is None:
                fallback = {"ean": digits, "lot": "", "ddm": None}
    if best is not None:
        return best
    return fallback  # type: ignore[return-value]


def extract_ean_from_image(image_bytes: bytes) -> str | None:
    """Décode un code-barres depuis les bytes d'une image (JPG/PNG/HEIC).

    Utilise ``zxing-cpp`` (wrapper natif C++ ZXing). Pour un GS1-128, le
    texte retourné est ``"(01)<GTIN>(15)<DDM>(10)<lot>"`` — on extrait juste
    le GTIN (AI 01) pour le matching produit, ou le code complet si ce n'est
    pas un GS1-128.

    Returns:
        Une string :
          - Si GS1-128 avec AI 01 : juste le GTIN-14 (14 digits)
          - Si EAN-13/UPC : le code tel quel
          - Sinon : le texte décodé brut
        ``None`` si rien décodé.
    """
    try:
        import io as _io

        import zxingcpp
        from PIL import Image
    except ImportError as exc:
        _log.error("zxing-cpp ou Pillow indisponible : %s", exc)
        return None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(_io.BytesIO(image_bytes))
            img.load()
            # Convertir en RGB si l'image est CMYK / RGBA / palette / etc.
            if img.mode not in ("L", "RGB"):
                img = img.convert("RGB")
            results = zxingcpp.read_barcodes(img)
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        _log.warning("Decompression bomb détectée — image rejetée")
        return None
    except Exception:
        _log.exception("Erreur décodage code-barres image")
        return None

    if not results:
        return None

    # Préférer le premier résultat valide. Pour un GS1-128 (Code 128 + AI),
    # le texte est de la forme "(01)<14 digits>(15)<...>(10)<...>".
    for r in results:
        text = (r.text or "").strip()
        if not text:
            continue
        # Si c'est un GS1-128 avec AI 01 → on extrait le GTIN
        m = re.match(r"^\(01\)(\d{14})", text)
        if m:
            return m.group(1)
        # Sinon on retourne le texte brut (digits si EAN/UPC)
        return text
    return None


def _find_uvc_for_product(
    raw_matrice: dict, id_produit: int, contenance_l: float,
) -> str:
    """Trouve le GTIN UVC (bouteille seule) pour un id_produit donné.

    Le matrice EB contient plusieurs codesBarres par produit (UVC +
    colis × N formats). On identifie l'UVC par :
      - même idProduit
      - même contenance (ex: 0.33 L pour 33cl)
      - libellé du lot sans nombre > 1 (ex: "Unité", "Bouteille")
        ou avec pkg=1 ("Carton de 1")

    Retourne le GTIN en digits (sans chiffres de contrôle ajoutés), ou
    "" si aucun UVC trouvé.
    """
    target_cont = round(float(contenance_l or 0), 2)
    for prod in (raw_matrice or {}).get("produits", []):
        for cb in prod.get("codesBarres", []):
            mp = cb.get("modeleProduit") or {}
            if int(mp.get("idProduit") or 0) != int(id_produit):
                continue
            mc = cb.get("modeleContenant") or {}
            cont = round(float(mc.get("contenance") or 0), 2)
            if abs(cont - target_cont) > 0.01:
                continue
            ml = cb.get("modeleLot") or {}
            libelle = (ml.get("libelle") or "").strip().lower()
            m = re.search(r"\d+", libelle)
            pkg = int(m.group(0)) if m else 0
            # UVC : libellé sans nombre, ou "Carton de 1"
            if pkg <= 1:
                code = re.sub(r"\D+", "", str(cb.get("code") or ""))
                if code:
                    return code
    return ""


def lookup_product_by_ean(ean: str) -> dict | None:
    """Cherche un produit dans la matrice codes-barres EasyBeer (cache 24 h).

    Source unique de vérité, indépendante de la sync étiquettes (qui peut
    être en retard d'une journée). Si le produit a été déclaré côté EasyBeer
    avec son EAN colis, ce lookup le trouvera immédiatement.

    Returns:
        Dict avec les clés : ``id_produit``, ``designation``, ``marque``,
        ``fmt``, ``pcb``, ``bottle_type``, ``gout``, ``ean_colis``,
        ``ean_uvc`` (peut être vide si pas trouvé dans la matrice).
        ``None`` si pas trouvé ou EasyBeer indisponible.
    """
    digits = re.sub(r"\D+", "", ean or "")
    if not digits:
        return None
    digits_13 = digits[-13:] if len(digits) > 13 else digits

    try:
        raw_matrice = get_code_barre_matrice()
    except (EasyBeerError, Exception) as exc:
        _log.warning("Lookup EAN : matrice CB EasyBeer indisponible (%s)", exc)
        return None
    cb_by_product = parse_barcode_matrix(raw_matrice)

    try:
        products_list = get_all_products() or []
    except (EasyBeerError, Exception):
        products_list = []
    label_by_id: dict[int, str] = {}
    for p in products_list:
        pid = p.get("idProduit")
        lbl = (p.get("libelle") or "").strip()
        if pid and lbl:
            label_by_id[int(pid)] = lbl

    for id_produit, formats in cb_by_product.items():
        for f in formats:
            full_code = (f.get("full_code") or "").strip()
            if not full_code:
                continue
            if full_code != digits and full_code != digits_13 and not full_code.endswith(digits_13):
                continue
            # Match !
            fmt = f.get("fmt_str") or ""
            m = re.match(r"(\d+)x", fmt)
            pcb = int(m.group(1)) if m else 0
            raw_label = label_by_id.get(id_produit, "") or ""
            designation = clean_product_label(raw_label)
            marque = determine_brand_from_label(raw_label)
            bottle_type = classify_bottle_type(designation, marque, pcb, fmt=fmt)
            gout = extract_label_gout(designation, marque, designation)
            # Calcul de la contenance pour retrouver l'UVC du même produit
            # (33cl → 0.33 L, 75cl → 0.75 L) depuis le format
            vol_m = re.search(r"x(\d+)", fmt)
            contenance_l = int(vol_m.group(1)) / 100.0 if vol_m else 0.0
            ean_uvc = _find_uvc_for_product(raw_matrice, id_produit, contenance_l)
            return {
                "id_produit": id_produit,
                "designation": designation,
                "marque": marque,
                "fmt": fmt,
                "pcb": pcb,
                "bottle_type": bottle_type,
                "gout": gout,
                "ean_colis": full_code,
                "ean_uvc": ean_uvc,
            }
    return None


def find_entry_by_ean(entries: list[LabelEntry], scanned_ean: str) -> LabelEntry | None:
    """Fallback : trouve une entrée dans la sync étiquettes à partir d'un EAN.

    Préférer ``lookup_product_by_ean`` (interroge la matrice EasyBeer, plus
    fraîche). Cette fonction reste utile quand la matrice EB est indisponible
    ou que le produit y a été déclaré sans EAN colis.

    Match par ordre de priorité :
      1. ``ean_colis`` exact (l'étiquette carton porte le GTIN du carton)
      2. ``ean_uvc`` exact (fallback : l'étiquette imprime parfois l'EAN bouteille)
      3. Suffixe : on compare les 13 derniers digits (cas EAN-13 vs GTIN-14)

    L'EAN scanné est nettoyé (digits seulement). Retourne ``None`` si pas trouvé.
    """
    digits = re.sub(r"\D+", "", scanned_ean or "")
    if not digits:
        return None

    # 1. Match colis exact
    for e in entries:
        if e.ean_colis == digits:
            return e
    # 2. Match UVC exact
    for e in entries:
        if e.ean_uvc and e.ean_uvc == digits:
            return e
    # 3. Match par suffixe 13 digits (gestion EAN-13 ↔ GTIN-14)
    suffix = digits[-13:]
    for e in entries:
        if e.ean_colis.endswith(suffix) or (e.ean_uvc and e.ean_uvc.endswith(suffix)):
            return e
    return None


def save_label_history(
    tenant_id: str,
    *,
    user_email: str,
    ean: str,
    lot: str,
    ddm: _dt.date,
    fmt: str,
    marque: str,
    designation: str,
    gout: str,
    case_count: int,
    full_pallet: bool,
    n_copies: int,
    pcb: int,
    gtin_uvc: str = "",
    code_interne: str = "",
    bio: bool = True,
    sscc: str = "",
) -> int | None:
    """Persiste une étiquette palette dans l'historique pour réimpression future.

    Fire-and-forget : log l'erreur et retourne None plutôt que de propager.
    """
    try:
        rows = run_sql(
            """INSERT INTO etiquette_palette_history
               (tenant_id, user_email, ean, lot, ddm, fmt, marque, designation,
                gout, case_count, full_pallet, n_copies, pcb,
                gtin_uvc, code_interne, bio, sscc)
               VALUES (:t, :u, :ean, :lot, :ddm, :fmt, :m, :des, :g,
                       :cc, :fp, :n, :pcb, :uvc, :ci, :bio, :sscc)
               RETURNING id""",
            {
                "t": tenant_id, "u": user_email, "ean": ean, "lot": lot,
                "ddm": ddm, "fmt": fmt, "m": marque, "des": designation,
                "g": gout, "cc": int(case_count), "fp": bool(full_pallet),
                "n": int(n_copies), "pcb": int(pcb),
                "uvc": gtin_uvc or "", "ci": code_interne or "",
                "bio": bool(bio), "sscc": sscc or "",
            },
        )
        return int(rows[0]["id"]) if rows else None
    except Exception:
        _log.exception("Échec sauvegarde historique étiquette palette (fire-and-forget)")
        return None


def list_recent_labels(tenant_id: str, limit: int = 20) -> list[HistoryEntry]:
    """Retourne les ``limit`` dernières étiquettes générées pour le tenant.

    Inclut le statut d'annulation (voided_at / voided_reason) via JOIN
    sur sscc_log — la source de vérité pour l'état "fantôme" est dans
    sscc_log (un seul endroit où on annule).
    """
    try:
        rows = run_sql(
            """SELECT eph.id, eph.ean, eph.lot, eph.ddm, eph.fmt, eph.marque,
                      eph.designation, eph.gout, eph.case_count,
                      eph.full_pallet, eph.n_copies, eph.pcb,
                      eph.gtin_uvc, eph.code_interne, eph.bio, eph.sscc,
                      eph.user_email, eph.generated_at, eph.archived_at,
                      sl.voided_at, sl.voided_reason
               FROM etiquette_palette_history eph
               LEFT JOIN sscc_log sl
                      ON sl.sscc = eph.sscc AND sl.tenant_id = eph.tenant_id
               WHERE eph.tenant_id = :t
               ORDER BY eph.generated_at DESC
               LIMIT :n""",
            {"t": tenant_id, "n": int(limit)},
        ) or []
    except Exception:
        _log.exception("Échec lecture historique étiquettes palette")
        return []

    out: list[HistoryEntry] = []
    for r in rows:
        try:
            out.append(HistoryEntry(
                id=int(r["id"]),
                ean=str(r["ean"] or ""),
                lot=str(r["lot"] or ""),
                ddm=r["ddm"] if isinstance(r["ddm"], _dt.date) else _dt.date.fromisoformat(str(r["ddm"])[:10]),
                fmt=str(r["fmt"] or ""),
                marque=str(r["marque"] or ""),
                designation=str(r["designation"] or ""),
                gout=str(r["gout"] or ""),
                case_count=int(r["case_count"] or 0),
                full_pallet=bool(r["full_pallet"]),
                n_copies=int(r["n_copies"] or 1),
                pcb=int(r["pcb"] or 0),
                gtin_uvc=str(r.get("gtin_uvc") or ""),
                code_interne=str(r.get("code_interne") or ""),
                bio=bool(r.get("bio", True)),
                user_email=str(r["user_email"] or ""),
                generated_at=r["generated_at"],
                sscc=str(r.get("sscc") or ""),
                voided_at=r.get("voided_at"),
                voided_reason=str(r.get("voided_reason") or ""),
                archived_at=r.get("archived_at"),
            ))
        except (KeyError, TypeError, ValueError):
            _log.warning("Ligne historique invalide ignorée : %r", r, exc_info=True)
    return out


def set_label_archived(
    tenant_id: str,
    label_id: int,
    *,
    archived: bool | None = None,
    reason: str | None = None,
) -> _dt.datetime | None | bool:
    """Archive/désarchive une étiquette palette historisée.

    - ``archived=True`` : force l'archivage (set archived_at = now())
    - ``archived=False`` : désarchive (set archived_at = NULL)
    - ``archived=None`` : toggle (utilisé par mobile pour le bouton uniforme)

    ``reason`` : motif d'archivage (Doublon, Erreur, Perte, texte libre…).
    Stocké uniquement quand la ligne finit *archivée* ; au désarchivage le
    motif est vidé (NULL) pour ne pas laisser un motif orphelin.

    L'étiquette doit appartenir au ``tenant_id`` fourni — sinon retourne False
    (sécurité multi-tenant : pas d'archivage cross-tenant).

    Returns:
        - datetime de l'archivage (ou None si désarchivée) si succès
        - False si label introuvable ou pas dans le bon tenant
    """
    reason_clean = (reason or "").strip() or None
    if archived is None:
        # Toggle : on calcule le nouvel état dans la même requête. Le motif
        # n'est conservé que si la ligne devient archivée.
        sql = """
            UPDATE etiquette_palette_history
            SET archived_at = CASE WHEN archived_at IS NULL THEN now() ELSE NULL END,
                archived_reason = CASE WHEN archived_at IS NULL THEN :r ELSE NULL END
            WHERE id = :id AND tenant_id = :t
            RETURNING archived_at
        """
        params = {"id": int(label_id), "t": tenant_id, "r": reason_clean}
    else:
        sql = """
            UPDATE etiquette_palette_history
            SET archived_at = CASE WHEN :a THEN now() ELSE NULL END,
                archived_reason = CASE WHEN :a THEN :r ELSE NULL END
            WHERE id = :id AND tenant_id = :t
            RETURNING archived_at
        """
        params = {
            "id": int(label_id), "t": tenant_id,
            "a": bool(archived), "r": reason_clean,
        }

    try:
        rows = run_sql(sql, params)
    except Exception:
        _log.exception("Échec set_label_archived id=%s tenant=%s", label_id, tenant_id)
        return False
    if not rows:
        return False
    return rows[0].get("archived_at")


def get_history_entry(tenant_id: str, entry_id: int) -> HistoryEntry | None:
    """Récupère une entrée d'historique par son id (pour la réimpression)."""
    rows = list_recent_labels(tenant_id, limit=1000)
    return next((e for e in rows if e.id == entry_id), None)


# Nb max de lignes d'historique à conserver par tenant. Au-delà, les plus
# anciennes sont supprimées par purge_old_label_history (appelée fire-and-forget
# après chaque INSERT). 500 = ~3-5 mois d'usage normal pour Symbiose.
_HISTORY_MAX_PER_TENANT = 500


def purge_old_label_history(tenant_id: str, keep: int = _HISTORY_MAX_PER_TENANT) -> int:
    """Supprime les entrées les plus anciennes au-delà de ``keep`` pour ce tenant.

    Fire-and-forget : log l'erreur sans propager. Appelée après chaque INSERT
    pour maintenir la table à taille bornée. Idempotent.

    Returns:
        Nb de lignes supprimées.
    """
    try:
        rows = run_sql(
            """DELETE FROM etiquette_palette_history
               WHERE tenant_id = :t
                 AND id NOT IN (
                   SELECT id FROM etiquette_palette_history
                   WHERE tenant_id = :t
                   ORDER BY generated_at DESC
                   LIMIT :keep
                 )
               RETURNING id""",
            {"t": tenant_id, "keep": int(keep)},
        ) or []
        n = len(rows)
        if n > 0:
            _log.info(
                "Purge historique étiquettes : %d lignes supprimées pour tenant %s",
                n, tenant_id,
            )
        return n
    except Exception:
        _log.exception("Échec purge historique étiquettes (fire-and-forget)")
        return 0


def count_today_and_month(tenant_id: str) -> dict[str, int]:
    """Compte les étiquettes générées aujourd'hui et ce mois pour le tenant.

    Exclut les archivées (``archived_at IS NULL``) — l'archivage est censé
    sortir l'étiquette des compteurs sans la supprimer. Une seule query
    pour les 2 compteurs via PostgreSQL FILTER WHERE.

    Returns:
        ``{"today_count": int, "month_count": int}``.
    """
    rows = run_sql(
        """
        SELECT
          COUNT(*) FILTER (WHERE generated_at::date = CURRENT_DATE
                             AND archived_at IS NULL)                  AS today_count,
          COUNT(*) FILTER (WHERE date_trunc('month', generated_at)
                              = date_trunc('month', now())
                             AND archived_at IS NULL)                  AS month_count
        FROM etiquette_palette_history
        WHERE tenant_id = :t
        """,
        {"t": tenant_id},
    ) or [{}]
    stats = rows[0]
    return {
        "today_count": int(stats.get("today_count") or 0),
        "month_count": int(stats.get("month_count") or 0),
    }


def list_today_labels(tenant_id: str) -> list[dict]:
    """Retourne les étiquettes du jour (générées aujourd'hui), tous statuts.

    Inclut les archivées (avec ``archived_at`` non null) pour permettre la
    réversibilité côté mobile. Triées par ``generated_at DESC``.

    Format dict (et non HistoryEntry) parce que c'est destiné directement
    à un payload JSON API — on évite une couche de conversion en plus.
    """
    rows = run_sql(
        """
        SELECT id, sscc, designation, marque, fmt, gout, lot, ddm,
               case_count, full_pallet, n_copies, generated_at, archived_at
        FROM etiquette_palette_history
        WHERE tenant_id = :t
          AND generated_at::date = CURRENT_DATE
        ORDER BY generated_at DESC
        """,
        {"t": tenant_id},
    ) or []
    return [
        {
            "id": int(r["id"]),
            "sscc": str(r.get("sscc") or ""),
            "designation": str(r.get("designation") or ""),
            "marque": str(r.get("marque") or ""),
            "fmt": str(r.get("fmt") or ""),
            "gout": str(r.get("gout") or ""),
            "lot": str(r.get("lot") or ""),
            "ddm": r["ddm"].isoformat() if r.get("ddm") else None,
            "case_count": int(r.get("case_count") or 0),
            "full_pallet": bool(r.get("full_pallet")),
            "n_copies": int(r.get("n_copies") or 1),
            "generated_at": r["generated_at"].isoformat() if r.get("generated_at") else None,
            "archived_at": r["archived_at"].isoformat() if r.get("archived_at") else None,
        }
        for r in rows
    ]


def _resolve_palette_label_fields(
    *,
    ean: str,
    designation: str | None,
    marque: str | None,
    fmt: str | None,
    pcb: int | None,
    gout: str | None,
    gtin_uvc: str | None,
) -> dict:
    """Centralise la résolution des champs produit pour la génération
    d'étiquette palette.

    Si TOUS les champs produit sont fournis (cas web qui a déjà sa LabelEntry),
    on les retourne tels quels. Si AU MOINS UN champ manque (cas mobile qui
    n'a que l'EAN), on fait un ``lookup_product_by_ean`` et on remplit les
    trous avec la matrice EasyBeer.

    Raises:
        ProductNotFoundError: si lookup nécessaire mais EAN absent.

    Returns:
        Dict normalisé : ``{designation, marque, fmt, pcb, gout, gtin_uvc}``.
    """
    needs_lookup = any(
        v is None for v in (designation, marque, fmt, pcb, gout, gtin_uvc)
    )
    if needs_lookup:
        product = lookup_product_by_ean(ean)
        if not product:
            raise ProductNotFoundError(
                f"Produit introuvable pour EAN {ean} dans la matrice EasyBeer"
            )
        designation = designation if designation is not None else product.get("designation") or ""
        marque = marque if marque is not None else product.get("marque") or ""
        fmt = fmt if fmt is not None else product.get("fmt") or ""
        pcb = pcb if pcb is not None else int(product.get("pcb") or 0)
        gout = gout if gout is not None else product.get("gout") or ""
        gtin_uvc = gtin_uvc if gtin_uvc is not None else product.get("ean_uvc") or ""
    return {
        "designation": designation or "",
        "marque": marque or "",
        "fmt": fmt or "",
        "pcb": int(pcb or 0),
        "gout": gout or "",
        "gtin_uvc": gtin_uvc or "",
    }


def _build_palette_pdf(
    *,
    ean: str,
    lot: str,
    ddm: _dt.date,
    case_count: int,
    full_pallet: bool,
    n_copies: int,
    sscc: str,
    fields: dict,
    code_interne: str,
    bio: bool,
    tenant_name: str,
) -> bytes:
    """Build pur du PDF étiquette palette. Pas d'I/O DB.
    Utilisé par ``generate_and_save_palette_label`` (vraie impression) ET par
    ``preview_palette_label`` (aperçu sans SSCC ni audit).
    """
    from common.etiquette_palette_pdf import EtiquetteContext, build_etiquette_palette_pdf

    ctx = EtiquetteContext(
        product_label=fields["designation"],
        fmt=fields["fmt"],
        ean13=ean,
        lot=lot,
        ddm=ddm,
        case_count=case_count,
        full_pallet=full_pallet,
        tenant_name=tenant_name,
        n_copies=n_copies,
        marque=fields["marque"],
        code_interne=code_interne,
        gtin_uvc=fields["gtin_uvc"],
        pcb=fields["pcb"],
        bio=bio,
        sscc=sscc,
    )
    return build_etiquette_palette_pdf(ctx)


def preview_palette_label(
    *,
    ean: str,
    lot: str,
    ddm: _dt.date,
    case_count: int,
    full_pallet: bool,
    n_copies: int = 1,
    designation: str | None = None,
    marque: str | None = None,
    fmt: str | None = None,
    pcb: int | None = None,
    gout: str | None = None,
    gtin_uvc: str | None = None,
    code_interne: str = "",
    bio: bool = True,
    tenant_name: str = "",
) -> bytes:
    """Génère un PDF d'étiquette palette en mode APERÇU.

    Différences avec ``generate_and_save_palette_label`` :
    - PAS d'appel à ``generate_sscc`` → aucune consommation de la séquence
      ``sscc_serial_seq`` (utile pour tests / formation sans polluer l'audit)
    - PAS d'INSERT dans ``etiquette_palette_history`` → aucun audit créé
    - Le SSCC affiché sur le PDF est laissé vide (``""``) → la section SSCC
      du PDF est masquée, l'opérateur voit que c'est un aperçu non-valide.

    Use case : valider visuellement le contenu (nom produit, lot, DDM, qty,
    format) avant de cliquer sur "Imprimer" qui fera la vraie génération.

    Raises:
        ProductNotFoundError: si lookup produit nécessaire et EAN absent.
    """
    fields = _resolve_palette_label_fields(
        ean=ean, designation=designation, marque=marque, fmt=fmt,
        pcb=pcb, gout=gout, gtin_uvc=gtin_uvc,
    )
    return _build_palette_pdf(
        ean=ean, lot=lot, ddm=ddm, case_count=case_count,
        full_pallet=full_pallet, n_copies=n_copies,
        sscc="",                        # ← aperçu : pas de SSCC
        fields=fields,
        code_interne=code_interne, bio=bio, tenant_name=tenant_name,
    )


def generate_and_save_palette_label(
    tenant_id: str,
    *,
    user_email: str,
    ean: str,
    lot: str,
    ddm: _dt.date,
    case_count: int,
    full_pallet: bool,
    n_copies: int = 1,
    # Champs produit optionnels — si non fournis, on fait un lookup_product_by_ean.
    # Le web les fournit (depuis sa LabelEntry chargée) ; le mobile les omet.
    designation: str | None = None,
    marque: str | None = None,
    fmt: str | None = None,
    pcb: int | None = None,
    gout: str | None = None,
    gtin_uvc: str | None = None,
    code_interne: str = "",
    bio: bool = True,
    tenant_name: str = "",
) -> tuple[bytes, str, int | None]:
    """Pipeline complet "génération d'une étiquette palette".

    Source de vérité **unique** pour web + mobile. Effectue dans l'ordre :
      1. Lookup produit si les champs produit ne sont pas fournis (mobile).
      2. Génère un SSCC unique (atomique via sequence Postgres ; audité dans sscc_log).
      3. Construit l'EtiquetteContext et le PDF (treepoem + fpdf2).
      4. Persiste l'audit dans ``etiquette_palette_history`` (fire-and-forget).
      5. Purge les anciennes entrées au-delà de la limite par tenant.

    Si la lookup produit échoue (EAN inconnu en matrice EasyBeer) et qu'aucun
    champ produit n'a été fourni → lève ``ProductNotFoundError``.

    Returns:
        ``(pdf_bytes, sscc, label_history_id | None)``. Le ``label_history_id``
        est ``None`` si l'INSERT history a échoué (DB down) — le PDF est
        quand même retourné pour respecter la logique fire-and-forget.

    Raises:
        ProductNotFoundError: si lookup nécessaire mais EAN absent de la matrice EB.
    """
    from common.services.sscc_service import generate_sscc

    # Étape 1 — Lookup produit si nécessaire (helper partagé avec preview).
    fields = _resolve_palette_label_fields(
        ean=ean, designation=designation, marque=marque, fmt=fmt,
        pcb=pcb, gout=gout, gtin_uvc=gtin_uvc,
    )

    # Étape 2 — SSCC. Best-effort : si la séquence échoue (DB down), on
    # continue avec un SSCC vide pour que l'opérateur puisse quand même
    # imprimer (mieux qu'aucune étiquette — le manque sera visible côté audit).
    try:
        sscc_result = generate_sscc(
            tenant_id,
            user_email=user_email,
            gtin_palette=ean,
            lot=lot,
            ddm=ddm,
            case_count=case_count,
        )
        sscc_str = sscc_result.sscc
    except Exception:
        _log.exception("Échec génération SSCC — étiquette imprimée sans SSCC")
        sscc_str = ""

    # Étape 3 — Build PDF (helper partagé avec preview).
    pdf_bytes = _build_palette_pdf(
        ean=ean, lot=lot, ddm=ddm, case_count=case_count,
        full_pallet=full_pallet, n_copies=n_copies,
        sscc=sscc_str,
        fields=fields,
        code_interne=code_interne, bio=bio, tenant_name=tenant_name,
    )

    # Étape 4 — Audit (fire-and-forget). Si l'INSERT échoue, on log mais on
    # ne propage pas — le PDF est déjà imprimable.
    label_id = save_label_history(
        tenant_id,
        user_email=user_email,
        ean=ean,
        lot=lot,
        ddm=ddm,
        fmt=fields["fmt"],
        marque=fields["marque"],
        designation=fields["designation"],
        gout=fields["gout"],
        case_count=case_count,
        full_pallet=full_pallet,
        n_copies=n_copies,
        pcb=fields["pcb"],
        gtin_uvc=fields["gtin_uvc"],
        code_interne=code_interne,
        bio=bio,
        sscc=sscc_str,
    )

    # Étape 5 — Purge (best-effort).
    try:
        purge_old_label_history(tenant_id)
    except Exception:
        _log.exception("Échec purge_old_label_history (non bloquant)")

    return pdf_bytes, sscc_str, label_id


class ProductNotFoundError(Exception):
    """Levée par generate_and_save_palette_label quand l'EAN est introuvable."""


def get_sync_status(tenant_id: str) -> SyncStatus:
    """Retourne l'âge et le statut de la dernière sync étiquettes du tenant."""
    rows = run_sql(
        """SELECT status, applied_at, created_at, product_count
           FROM sync_operations
           WHERE tenant_id = :t AND status IN ('applied', 'pending', 'fetched')
           ORDER BY (status = 'applied') DESC, created_at DESC
           LIMIT 1""",
        {"t": tenant_id},
    )
    if not rows:
        return SyncStatus(has_sync=False, age_hours=None, status=None, product_count=0)

    op = rows[0]
    ref_dt = op.get("applied_at") or op.get("created_at")
    age_hours: float | None = None
    if ref_dt is not None:
        try:
            now = _dt.datetime.now(ref_dt.tzinfo) if ref_dt.tzinfo else _dt.datetime.now()
            age_hours = (now - ref_dt).total_seconds() / 3600.0
        except (TypeError, AttributeError):
            age_hours = None

    return SyncStatus(
        has_sync=True,
        age_hours=age_hours,
        status=op.get("status"),
        product_count=int(op.get("product_count") or 0),
    )


def trigger_sync_now(tenant_id: str) -> dict:
    """Déclenche une nouvelle sync étiquettes (collecte EasyBeer + insertion DB).

    Bloquant — utilise ``asyncio.to_thread`` côté UI pour ne pas figer l'event loop.
    Retourne {"id": <op_id>, "product_count": N} ou {"id": None, "product_count": 0}
    si EasyBeer ne renvoie aucun brassin actif.
    """
    from common.sync import create_sync_operation
    from common.sync.collector import collect_label_data

    products = collect_label_data()
    if not products:
        return {"id": None, "product_count": 0}
    op = create_sync_operation(products, tenant_id=tenant_id, triggered_by="manual")
    return op
