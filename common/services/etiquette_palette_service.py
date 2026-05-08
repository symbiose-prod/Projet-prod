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
import json
import logging
import re
from dataclasses import dataclass

from common.ramasse import clean_product_label, extract_gout, get_palette_layout
from db.conn import run_sql

_log = logging.getLogger("ferment.services.etiquette_palette")


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

    Si ``full_pallet`` est vrai, retourne ``layers × per_layer`` du layout
    correspondant (avec override de marque si applicable).

    Sinon : ``layers_full × per_layer + extras_top``, en validant que :
      - ``0 ≤ layers_full ≤ layers_max``
      - ``0 ≤ extras_top < per_layer`` (un étage plein → on incrémente layers_full)

    Raises:
        ValueError: format inconnu, ou valeurs hors bornes.
    """
    layout = get_palette_layout(fmt, product_label)
    if layout["total"] <= 0:
        raise ValueError(f"Format de palette inconnu : {fmt!r}")

    if full_pallet:
        return layout["total"]

    layers_max = layout["layers"]
    per_layer = layout["per_layer"]

    if not (0 <= layers_full <= layers_max):
        raise ValueError(
            f"layers_full doit être entre 0 et {layers_max} pour le format {fmt!r}",
        )
    if not (0 <= extras_top < per_layer):
        raise ValueError(
            f"extras_top doit être entre 0 et {per_layer - 1} pour le format {fmt!r} "
            f"(un étage complet → augmenter layers_full)",
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

def classify_bottle_type(designation: str, marque: str, pcb: int | float) -> str | None:
    """Classifie un produit en 33cl / 75cl SAFT / 75cl Eau gazeuse.

    Règles :
      - 33cl si la désignation contient "33cl"
      - 75cl SAFT si NIKO ou si Symbiose en PCB=4
      - 75cl Eau gazeuse si Symbiose en PCB=6
      - None si on ne peut pas conclure (rare, log warning)
    """
    desig = (designation or "").lower()
    pcb_int = int(pcb or 0)
    marque_up = (marque or "").upper()

    if "33cl" in desig or "33 cl" in desig:
        return BOTTLE_33
    if "75cl" in desig or "75 cl" in desig:
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


def find_entry_by_ean(entries: list[LabelEntry], scanned_ean: str) -> LabelEntry | None:
    """Trouve une entrée à partir d'un EAN scanné.

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
