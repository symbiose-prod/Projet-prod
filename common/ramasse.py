"""
common/ramasse.py
=================
Logique metier pour la Fiche de ramasse.

- Config poids cartons (remplace info_FDR.csv)
- Parsing matrice codes-barres EasyBeer
- Construction des lignes du tableau
- Utilitaires (date, nettoyage labels, etc.)
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from dateutil.tz import gettz

from common.easybeer import (
    get_brassin_detail,
    get_planification_matrice,
)

# ─── Config poids cartons ────────────────────────────────────────────────────
# 5 valeurs distinctes extraites de l'ancien info_FDR.csv.

# Fallback statique si EasyBeer est indisponible
CARTON_WEIGHTS_FALLBACK: dict[str, float] = {
    "12x33": 6.741,
    "6x75":  7.23,
    "4x75":  4.68,
}

WEIGHT_OVERRIDES_FALLBACK: dict[str, dict[str, float]] = {
    "6x75": {
        "niko": 6.84,
    },
}

# ─── Config palettes ────────────────────────────────────────────────────────
# Nombre de cartons par palette, par format.

PALETTE_EMPTY_WEIGHT: float = 25.0  # kg

PALETTE_CAPACITY: dict[str, int] = {
    "12x33": 126,
    "6x75":  96,   # Eaugazeuse (Verralia) par défaut
    "4x75":  112,  # SAFT
}

PALETTE_CAPACITY_OVERRIDES: dict[str, dict[str, int]] = {
    "6x75": {
        "niko": 84,  # SAFT (Niko)
    },
}


def get_carton_weight(
    fmt: str,
    product_label: str,
    *,
    id_produit: int | None = None,
    eb_weights: dict[tuple[int, str], float] | None = None,
) -> float:
    """
    Retourne le poids d'un carton pour un format et un produit donnes.

    Logique :
      1. Si eb_weights fourni (depuis EasyBeer), cherche (idProduit, fmt)
      2. Sinon fallback sur les constantes statiques
    """
    fmt_key = fmt.lower().replace("cl", "").replace(" ", "")

    # 1) Lookup dynamique EasyBeer
    if eb_weights and id_produit:
        poids = eb_weights.get((id_produit, fmt_key))
        if poids and poids > 0:
            return poids

    # 2) Fallback statique
    label_lower = _canon(product_label)
    overrides = WEIGHT_OVERRIDES_FALLBACK.get(fmt_key, {})
    for keyword, weight in overrides.items():
        if keyword in label_lower:
            return weight

    return CARTON_WEIGHTS_FALLBACK.get(fmt_key, 0.0)


def get_palette_capacity(fmt: str, product_label: str) -> int:
    """
    Retourne le nombre de cartons par palette pour un format et produit donnes.

    Logique identique a get_carton_weight :
      - Overrides par mot-cle (ex: Niko -> SAFT -> 84)
      - Sinon valeur par defaut du format
    """
    fmt_key = fmt.lower().replace("cl", "").replace(" ", "")
    label_lower = _canon(product_label)
    overrides = PALETTE_CAPACITY_OVERRIDES.get(fmt_key, {})
    for keyword, cap in overrides.items():
        if keyword in label_lower:
            return cap
    return PALETTE_CAPACITY.get(fmt_key, 0)


# ─── Destinataires ───────────────────────────────────────────────────────────

_DESTINATAIRES_PATH = Path(__file__).resolve().parent.parent / "data" / "destinataires.json"
_destinataires_cache: list[dict[str, Any]] | None = None


def load_destinataires() -> list[dict[str, Any]]:
    """Charge la liste des destinataires depuis data/destinataires.json (cached)."""
    global _destinataires_cache
    if _destinataires_cache is not None:
        return _destinataires_cache

    if not _DESTINATAIRES_PATH.exists():
        _destinataires_cache = []
        return _destinataires_cache

    with open(_DESTINATAIRES_PATH, encoding="utf-8") as f:
        data = json.load(f)

    _destinataires_cache = data.get("destinataires", [])
    return _destinataires_cache


# ─── Utilitaires texte ───────────────────────────────────────────────────────

def today_paris() -> dt.date:
    """Date courante timezone Europe/Paris."""
    return dt.datetime.now(gettz("Europe/Paris")).date()


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _canon(s: str) -> str:
    """Canonise un texte : sans accents, minuscules, espaces normalises."""
    s = _strip_accents(str(s or "")).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_product_label(raw_label: str) -> str:
    """
    Nettoie le libelle produit EasyBeer : supprime le suffixe degre (ex. '- 0.0').
    'Kefir Peche - 0.0' -> 'Kefir Peche'
    """
    label = str(raw_label or "").strip()
    label = re.sub(r"\s*-\s*\d+[\.,]?\d*\s*°\s*$", "", label).strip()
    return label


def extract_gout(product_label: str) -> str:
    """
    Extrait le gout depuis le libelle produit EasyBeer.
    'Kefir Gingembre' -> 'Gingembre'
    """
    label = clean_product_label(product_label)
    for prefix in [
        "Infusion de Kéfir de fruits",
        "Infusion de Kéfir",
        "Infusion probiotique",
        "Kéfir de fruits",
        "Kéfir",
    ]:
        if label.lower().startswith(prefix.lower()):
            return label[len(prefix):].strip()
    return label


def format_from_stock(stock_txt: str) -> str | None:
    """Detecte 12x33 / 6x75 / 4x75 dans un libelle de Stock ou conditionnement."""
    if not stock_txt:
        return None
    s = str(stock_txt).lower().replace("×", "x").replace("\u00a0", " ")

    vol = None
    if "0.33" in s or re.search(r"33\s*c?l", s):
        vol = 33
    elif "0.75" in s or re.search(r"75\s*c?l", s):
        vol = 75

    nb = None
    m = re.search(r"(?:carton|pack)\s*de\s*(12|6|4)\b", s)
    if not m:
        m = re.search(r"\b(12|6|4)\b", s)
    if m:
        nb = int(m.group(1))

    if vol == 33 and nb == 12:
        return "12x33"
    if vol == 75 and nb == 6:
        return "6x75"
    if vol == 75 and nb == 4:
        return "4x75"
    return None


# ─── Parsing matrice codes-barres ────────────────────────────────────────────

def parse_barcode_matrix(raw_matrice: dict) -> dict[int, list[dict]]:
    """
    Parse la reponse brute de get_code_barre_matrice() et retourne un index
    par produit :

        {idProduit: [{"ref6": "427014", "fmt_str": "12x33",
                       "full_code": "3770014427014", "lot_label": "Carton de 12"}, ...]}
    """
    by_product: dict[int, list[dict]] = {}
    for prod_entry in raw_matrice.get("produits", []):
        for cb in prod_entry.get("codesBarres", []):
            code_raw = str(cb.get("code") or "")
            id_produit = (cb.get("modeleProduit") or {}).get("idProduit")
            mod_cont = cb.get("modeleContenant") or {}
            contenance = round(float(mod_cont.get("contenance") or 0), 2)
            mod_lot = cb.get("modeleLot") or {}
            lot_libelle = (mod_lot.get("libelle") or "").strip()

            if not (id_produit and code_raw and contenance):
                continue

            digits = re.sub(r"\D+", "", code_raw)
            ref6 = digits[-6:] if len(digits) >= 6 else digits
            if not ref6:
                continue

            # Deriver le format depuis contenance + lot
            vol_cl = int(contenance * 100)  # 0.33 -> 33, 0.75 -> 75
            m_pkg = re.search(r"(\d+)", lot_libelle)
            pkg_count = int(m_pkg.group(1)) if m_pkg else 0
            if not (vol_cl and pkg_count):
                continue
            fmt_str = f"{pkg_count}x{vol_cl}"

            by_product.setdefault(id_produit, []).append({
                "ref6": ref6,
                "fmt_str": fmt_str,
                "full_code": digits,
                "lot_label": lot_libelle,
            })
    return by_product


# ─── Construction des lignes ─────────────────────────────────────────────────

def build_ramasse_lines(
    selected_brassins: list[dict],
    id_entrepot: int | None,
    cb_by_product: dict[int, list[dict]] | None = None,
    eb_weights: dict[tuple[int, str], float] | None = None,
) -> tuple[list[dict], dict]:
    """
    Pour chaque brassin selectionne :
      1. Charge le detail pour DDM et quantites existantes
      2. Charge la matrice pour les produits derives
      3. Pour chaque produit (principal + derives), interroge la matrice
         codes-barres pour savoir quels formats existent

    Retourne (rows, meta_by_label) prets pour le DataFrame.
    """
    rows: list[dict] = []
    meta_by_label: dict = {}
    seen: set[str] = set()

    for brassin_summary in selected_brassins:
        id_brassin = brassin_summary.get("idBrassin")
        if not id_brassin:
            continue

        brassin_produit = brassin_summary.get("produit") or {}

        # --- Detail du brassin (pour DDM et quantites existantes) ---
        try:
            detail = get_brassin_detail(id_brassin)
        except Exception:
            detail = brassin_summary

        # DDM calculee = date debut fermentation + 365 jours
        ddm_date = today_paris() + dt.timedelta(days=365)
        _raw_debut = detail.get("dateDebutFormulaire")
        if _raw_debut:
            try:
                if isinstance(_raw_debut, (int, float)):
                    ddm_date = dt.date.fromtimestamp(_raw_debut / 1000) + dt.timedelta(days=365)
                else:
                    ddm_date = dt.date.fromisoformat(str(_raw_debut)[:10]) + dt.timedelta(days=365)
            except (ValueError, TypeError, OSError):
                pass

        # Index des quantites existantes : (prod_libelle_lower, fmt_str) -> quantite
        _existing_prods = detail.get("productions") or detail.get("planificationsProductions") or []
        _existing_qty: dict[tuple[str, str], int] = {}
        for _pe in _existing_prods:
            _pe_label = ((_pe.get("produit") or {}).get("libelle") or "").lower()
            _pe_cond = str(_pe.get("conditionnement") or "")
            _pe_fmt = format_from_stock(_pe_cond) or format_from_stock(_pe_label)
            _pe_qty = int(_pe.get("quantite") or 0)
            if _pe_label and _pe_fmt:
                _existing_qty[(_pe_label, _pe_fmt)] = _pe_qty

        # --- Matrice EasyBeer : produits derives ---
        produits_derives: list[dict] = []
        if id_entrepot:
            try:
                matrice = get_planification_matrice(id_brassin, id_entrepot)
                produits_derives = matrice.get("produitsDerives", [])
            except Exception:
                pass

        # --- Tous les produits : principal + derives ---
        all_products: list[dict] = [brassin_produit]
        for pd_item in produits_derives:
            if pd_item.get("libelle"):
                all_products.append(pd_item)

        # --- Generer les lignes depuis la matrice codes-barres ---
        for prod in all_products:
            prod_label = (prod.get("libelle") or "").strip()
            id_produit = prod.get("idProduit")
            if not prod_label or not id_produit:
                continue

            clean_label = clean_product_label(prod_label)

            # Formats existants depuis la matrice codes-barres
            formats = (cb_by_product or {}).get(id_produit, [])

            for pf in formats:
                ref = pf["ref6"]
                fmt_str = pf["fmt_str"]

                label = f"{clean_label} — {fmt_str}cl"
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)

                # Poids carton (dynamique EasyBeer ou fallback)
                poids_carton = get_carton_weight(
                    fmt_str, clean_label,
                    id_produit=id_produit, eb_weights=eb_weights,
                )

                # Capacite palette (cartons/pal)
                palette_cap = get_palette_capacity(fmt_str, clean_label)

                # Quantite pre-remplie depuis productions existantes
                qty = _existing_qty.get((prod_label.lower(), fmt_str), 0)

                meta_by_label[label] = {
                    "_format": fmt_str,
                    "_poids_carton": poids_carton,
                    "_palette_capacity": palette_cap,
                    "_reference": ref,
                }
                rows.append({
                    "Référence": ref,
                    "Produit (goût + format)": label,
                    "DDM": ddm_date,
                    "Quantité cartons": qty,
                    "Quantité palettes": 0,
                    "Poids palettes (kg)": 0,
                })

    return rows, meta_by_label
