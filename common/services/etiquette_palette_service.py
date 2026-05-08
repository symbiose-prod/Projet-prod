"""
common/services/etiquette_palette_service.py
============================================
Service domaine : étiquettes palette logistique avec code-barres GS1-128.

Flow métier :
  1. L'opérateur sélectionne un produit + format → on récupère l'EAN-13 caisse
     depuis la matrice codes-barres EasyBeer (cache L2 24 h).
  2. Il sélectionne un brassin actif → le code brassin sert de lot, et la DDM
     est calculée depuis la date encodée dans le code brassin + ``ddm_days``.
  3. Il indique soit "palette pleine", soit (étages pleins + caisses sur le
     dernier étage) — on calcule le nombre total de caisses.
  4. On construit une chaîne d'Application Identifiers GS1 :
        (01)<GTIN-14> (15)<YYMMDD> (37)<count, padding 3> (10)<lot>
     encodable directement en Code 128 (sans FNC1 — la séparation des AI
     variables est garantie par le padding fixe sur AI 37 et le placement
     de AI 10 en dernier).

Le module est sans NiceGUI : utilisable depuis CLI / cron / tests.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from common.brassin_builder import extract_date_from_brassin_code
from common.data import get_business_config
from common.easybeer import (
    EasyBeerError,
    get_all_products,
    get_brassins_en_cours_cached,
    get_code_barre_matrice,
)
from common.ramasse import clean_product_label, get_palette_layout, parse_barcode_matrix

_log = logging.getLogger("ferment.services.etiquette_palette")


# ─── Modèles typés ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProductFormat:
    """Un combo (produit × format) avec son EAN-13 caisse."""
    id_produit: int
    libelle: str           # libellé produit nettoyé (ex: "Kéfir Mangue Passion")
    fmt: str               # ex: "12x33", "6x75"
    ean13: str             # 13 digits
    lot_label: str         # ex: "Carton de 12" (issu de la matrice EasyBeer)


@dataclass(frozen=True)
class BrassinChoice:
    """Brassin sélectionnable comme source du lot + DDM."""
    id_brassin: int
    code: str              # ex: "KME27042026" — sert de lot par défaut
    libelle_produit: str   # libellé du produit du brassin (info contextuelle)
    ddm_date: _dt.date     # date métier extraite du code + ddm_days


@dataclass(frozen=True)
class EtiquettePaletteData:
    """Données chargées au boot de la page (1 seul aller-retour EB groupé)."""
    products: list[ProductFormat]
    brassins: list[BrassinChoice]
    errors: list[str]


@dataclass(frozen=True)
class Gs1Payload:
    """Payload GS1-128 prêt pour l'encodage."""
    content: str           # chaîne à encoder en Code 128 (sans parenthèses)
    hri: str               # version lisible humainement avec parenthèses


# ─── Constantes ──────────────────────────────────────────────────────────────

# Padding du compteur AI 37 : 3 digits suffisent (palette max ~252 caisses).
# Permet de séparer AI 37 de AI 10 sans FNC1.
_AI37_WIDTH = 3
_LOT_MAX_LEN = 20          # contrainte GS1 sur AI 10
_LOT_ALLOWED_RE = re.compile(r"[^A-Z0-9\-./]")  # GS1 AI 10 = subset ASCII


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

def _ean13_to_gtin14(ean13: str) -> str:
    """Préfixe un EAN-13 avec '0' pour obtenir un GTIN-14 (logistic indicator)."""
    digits = re.sub(r"\D+", "", ean13 or "")
    if len(digits) == 14:
        return digits
    if len(digits) == 13:
        return "0" + digits
    raise ValueError(f"EAN/GTIN invalide (attendu 13 ou 14 digits) : {ean13!r}")


def _normalize_lot(lot: str) -> str:
    """Normalise un lot pour AI 10 : majuscules, ASCII restreint, longueur ≤ 20.

    Caractères autorisés en GS1 AI 10 : A-Z 0-9 et un sous-ensemble de
    ponctuation. On filtre tout le reste (accents, espaces, etc.) plutôt que
    de rejeter — les codes brassin sont déjà ``[A-Z0-9]+`` par construction.
    """
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
    """Construit la chaîne GS1 (AI 01 + 15 + 37 + 10) prête à encoder.

    Ordre des AI :
      - 01 (GTIN-14, 14 digits fixes)
      - 15 (DDM YYMMDD, 6 digits fixes)
      - 37 (count, padding sur ``_AI37_WIDTH`` digits)
      - 10 (lot, variable, en DERNIER pour ne pas avoir besoin de FNC1)

    Returns:
        Gs1Payload(content=concat sans parenthèses, hri=version lisible).
    """
    if count <= 0:
        raise ValueError("count doit être > 0")
    max_count = 10 ** _AI37_WIDTH - 1
    if count > max_count:
        raise ValueError(f"count > {max_count} (incrémenter _AI37_WIDTH)")

    gtin14 = _ean13_to_gtin14(ean13)
    yymmdd = ddm.strftime("%y%m%d")
    count_str = str(count).zfill(_AI37_WIDTH)
    lot_norm = _normalize_lot(lot)

    content = f"01{gtin14}15{yymmdd}37{count_str}10{lot_norm}"
    hri = f"(01){gtin14} (15){yymmdd} (37){count_str} (10){lot_norm}"
    return Gs1Payload(content=content, hri=hri)


# ─── Calcul DDM depuis code brassin ──────────────────────────────────────────

def compute_ddm_from_brassin_code(code: str | None) -> _dt.date | None:
    """Calcule la DDM = date_brassin + business.ddm_days.

    Retourne ``None`` si le code ne suit pas le pattern attendu (..DDMMYYYY).
    """
    base = extract_date_from_brassin_code(code)
    if base is None:
        return None
    ddm_days = int((get_business_config() or {}).get("ddm_days", 365))
    return base + _dt.timedelta(days=ddm_days)


# ─── Chargement initial (matrice CB + brassins en cours) ─────────────────────

def _load_products_with_formats() -> list[ProductFormat]:
    """Construit la liste produits×formats depuis EasyBeer.

    Combine ``get_code_barre_matrice()`` (donne EAN par idProduit + format) et
    ``get_all_products()`` (donne les libellés par idProduit). Les produits
    sans EAN ne sont pas exposés (rien à imprimer).
    """
    raw_matrice = get_code_barre_matrice()
    cb_by_product = parse_barcode_matrix(raw_matrice)

    # Index libellé par idProduit
    products_list = get_all_products() or []
    label_by_id: dict[int, str] = {}
    for p in products_list:
        pid = p.get("idProduit")
        lbl = (p.get("libelle") or "").strip()
        if pid and lbl:
            label_by_id[int(pid)] = lbl

    out: list[ProductFormat] = []
    for id_produit, formats in cb_by_product.items():
        libelle = clean_product_label(label_by_id.get(id_produit, ""))
        if not libelle:
            continue
        for f in formats:
            out.append(ProductFormat(
                id_produit=id_produit,
                libelle=libelle,
                fmt=f["fmt_str"],
                ean13=f["full_code"],
                lot_label=f.get("lot_label", ""),
            ))

    out.sort(key=lambda x: (x.libelle.lower(), x.fmt))
    return out


def _load_active_brassins() -> list[BrassinChoice]:
    """Charge les brassins en cours (ceux qui peuvent être conditionnés)."""
    raw = get_brassins_en_cours_cached() or []
    out: list[BrassinChoice] = []
    for b in raw:
        id_brassin = b.get("idBrassin")
        nom = (b.get("nom") or "").strip()
        if not id_brassin or not nom:
            continue
        produit = (b.get("produit") or {}).get("libelle") or ""
        ddm = compute_ddm_from_brassin_code(nom)
        if ddm is None:
            # Code brassin sans pattern DDMMYYYY → DDM par défaut = aujourd'hui + ddm_days
            ddm_days = int((get_business_config() or {}).get("ddm_days", 365))
            ddm = _dt.date.today() + _dt.timedelta(days=ddm_days)
        out.append(BrassinChoice(
            id_brassin=int(id_brassin),
            code=nom,
            libelle_produit=clean_product_label(produit),
            ddm_date=ddm,
        ))
    out.sort(key=lambda x: x.code)
    return out


def load_initial_data() -> EtiquettePaletteData:
    """Charge produits×formats et brassins en cours en parallèle.

    Tolérant : si un fetch EasyBeer échoue (transport ou rate-limit), on
    renvoie une liste vide pour ce volet et on accumule l'erreur — la page
    reste utilisable en mode dégradé (l'opérateur peut saisir lot/DDM à la main
    si la liste de brassins est vide).
    """
    errors: list[str] = []
    products: list[ProductFormat] = []
    brassins: list[BrassinChoice] = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_products = pool.submit(_load_products_with_formats)
        f_brassins = pool.submit(_load_active_brassins)

        try:
            products = f_products.result()
        except (EasyBeerError, requests.RequestException, OSError) as exc:
            _log.warning("Échec chargement produits×formats EasyBeer", exc_info=True)
            errors.append(f"Produits indisponibles : {exc}")

        try:
            brassins = f_brassins.result()
        except (EasyBeerError, requests.RequestException, OSError) as exc:
            _log.warning("Échec chargement brassins en cours EasyBeer", exc_info=True)
            errors.append(f"Brassins indisponibles : {exc}")

    return EtiquettePaletteData(products=products, brassins=brassins, errors=errors)
