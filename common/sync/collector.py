"""
common/sync/collector.py
========================
Collecteur de données étiquetage depuis EasyBeer.

Interroge les brassins en cours, produits dérivés, matrice codes-barres et
stocks produit fini pour construire la liste complète des produits à
synchroniser vers la base Access de l'imprimante d'étiquettes.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
from typing import Any

from common.easybeer.brassins import get_brassin_detail, get_brassins_en_cours
from common.easybeer.conditioning import get_code_barre_matrice
from common.easybeer.products import get_product_detail
from common.easybeer.stocks import get_stock_produit_detail
from common.easybeer._client import BASE, _auth, _check_response, _safe_json, get_session, TIMEOUT, EasyBeerError
from common.ramasse import clean_product_label

_log = logging.getLogger("ferment.sync")

# DDM par défaut = date brassage + 365 jours
_DDM_DAYS = 365


# ─── Étape 1 : Brassins en cours ────────────────────────────────────────────


def _fetch_active_brassins() -> list[dict[str, Any]]:
    """Récupère les brassins en cours, filtre annulés/terminés/trop petits."""
    brassins = get_brassins_en_cours()
    active = []
    for b in brassins:
        if b.get("annule") or b.get("termine"):
            continue
        vol = float(b.get("volume") or 0)
        if vol < 100:
            continue
        active.append(b)
    _log.info("Brassins en cours : %d actifs sur %d total", len(active), len(brassins))
    return active


# ─── Étape 2 : DDM et Lot depuis le détail brassin ──────────────────────────


def _compute_ddm(detail: dict[str, Any]) -> dt.date:
    """Calcule la DDM = date début brassin + 365 jours.

    Le champ dateDebutFormulaire est un timestamp ms (int/float) ou une string ISO.
    Fallback : date du jour + 365 jours.
    """
    raw_debut = detail.get("dateDebutFormulaire")
    if not raw_debut:
        _log.warning("Brassin sans dateDebutFormulaire, fallback DDM=today+365")
        return dt.date.today() + dt.timedelta(days=_DDM_DAYS)
    try:
        if isinstance(raw_debut, (int, float)):
            base = dt.date.fromtimestamp(raw_debut / 1000)
        else:
            base = dt.date.fromisoformat(str(raw_debut)[:10])
        return base + dt.timedelta(days=_DDM_DAYS)
    except (ValueError, TypeError, OSError):
        _log.warning("Impossible de parser dateDebutFormulaire=%r, fallback", raw_debut)
        return dt.date.today() + dt.timedelta(days=_DDM_DAYS)


def _ddm_to_lot(ddm: dt.date) -> int:
    """Formate la DDM en numéro de lot DDMMYYYY (ex: 11032027)."""
    return int(ddm.strftime("%d%m%Y"))


# ─── Étape 3 : Produit + dérivés ────────────────────────────────────────────


def _get_all_product_ids(id_produit: int) -> list[int]:
    """Retourne [produit principal] + tous les produits dérivés (NIKO, Export, etc.)."""
    ids = [id_produit]
    try:
        detail = get_product_detail(id_produit)
        derived = detail.get("idsProduitsDerives") or []
        for did in derived:
            if isinstance(did, int) and did not in ids:
                ids.append(did)
    except Exception:
        _log.warning("Impossible de charger les dérivés du produit %s", id_produit, exc_info=True)
    return ids


# ─── Étape 4 : Matrice codes-barres → GTIN + PCB ────────────────────────────


def _parse_barcode_matrix_for_labels(
    raw_matrice: dict[str, Any],
) -> dict[int, dict[str, dict[str, Any]]]:
    """Parse la matrice codes-barres et organise par produit et format.

    Retourne :
        {idProduit: {fmt_str: {"gtin_uvc": str, "gtin_colis": str, "pcb": int}}}

    Distinction GTIN UVC / GTIN Colis :
      - modeleLot.quantite == 1  → GTIN UVC (bouteille individuelle)
      - modeleLot.quantite > 1   → GTIN Colis (carton), PCB = quantite
    """
    by_product: dict[int, dict[str, dict[str, Any]]] = {}

    for prod_entry in raw_matrice.get("produits", []):
        for cb in prod_entry.get("codesBarres", []):
            code_raw = str(cb.get("code") or "").strip()
            id_produit = (cb.get("modeleProduit") or {}).get("idProduit")
            mod_cont = cb.get("modeleContenant") or {}
            contenance = round(float(mod_cont.get("contenance") or 0), 2)
            mod_lot = cb.get("modeleLot") or {}
            lot_libelle = (mod_lot.get("libelle") or "").strip()
            lot_qty = int(mod_lot.get("quantite") or 0)

            if not (id_produit and code_raw and contenance):
                continue

            digits = re.sub(r"\D+", "", code_raw)
            if len(digits) < 8:  # EAN8 minimum
                continue

            vol_cl = int(contenance * 100)  # 0.33 → 33, 0.75 → 75

            if lot_qty <= 0:
                # Essai extraction depuis le libellé (ex: "Carton de 12")
                m = re.search(r"(\d+)", lot_libelle)
                if m:
                    lot_qty = int(m.group(1))
                else:
                    continue

            if lot_qty == 1:
                # C'est un GTIN UVC (bouteille individuelle)
                # On ne connaît pas encore le format carton, on stocke temporairement
                # Le fmt_str sera associé via le volume
                for fmt_str, info in by_product.get(id_produit, {}).items():
                    # Matcher sur le volume (même contenance)
                    fmt_vol = re.search(r"x(\d+)", fmt_str)
                    if fmt_vol and int(fmt_vol.group(1)) == vol_cl:
                        info["gtin_uvc"] = digits
                # Stocker aussi en clé spéciale pour association ultérieure
                by_product.setdefault(id_produit, {})
                by_product[id_produit].setdefault(f"_uvc_{vol_cl}", {})["gtin_uvc"] = digits
            else:
                # C'est un GTIN Colis (carton)
                fmt_str = f"{lot_qty}x{vol_cl}"
                by_product.setdefault(id_produit, {})
                entry = by_product[id_produit].setdefault(fmt_str, {
                    "gtin_uvc": "",
                    "gtin_colis": "",
                    "pcb": lot_qty,
                })
                entry["gtin_colis"] = digits
                entry["pcb"] = lot_qty

    # Deuxième passe : associer les GTIN UVC aux formats
    for id_produit, formats in by_product.items():
        uvc_keys = [k for k in formats if k.startswith("_uvc_")]
        for uvc_key in uvc_keys:
            vol_cl_str = uvc_key.split("_")[-1]
            gtin_uvc = formats[uvc_key].get("gtin_uvc", "")
            if gtin_uvc:
                for fmt_str, info in formats.items():
                    if fmt_str.startswith("_"):
                        continue
                    fmt_vol = re.search(r"x(\d+)", fmt_str)
                    if fmt_vol and fmt_vol.group(1) == vol_cl_str and not info.get("gtin_uvc"):
                        info["gtin_uvc"] = gtin_uvc
            del formats[uvc_key]

    return by_product


# ─── Étape 5 : CODE INTERNE (codeArticle) depuis stocks ─────────────────────


def _fetch_stock_codes() -> dict[tuple[int, str], str]:
    """Récupère les codeArticle depuis POST /stock/produits.

    Retourne {(idProduit, fmt_str): codeArticle}.
    Suit le pattern de fetch_carton_weights() dans common/easybeer/stocks.py.
    """
    id_brasserie = int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013"))
    payload = {"idBrasserie": id_brasserie}

    try:
        r = get_session().post(
            f"{BASE}/stock/produits",
            json=payload,
            auth=_auth(),
            timeout=TIMEOUT,
        )
        _check_response(r, "stock/produits")
        data = _safe_json(r, "stock/produits")
    except (EasyBeerError, Exception):
        _log.exception("Erreur fetch stock/produits pour codes articles")
        return {}

    codes: dict[tuple[int, str], str] = {}

    for prod in data.get("consolidationsFilles", []):
        for conso in prod.get("consolidationsFilles", []):
            sid = conso.get("id")
            if not sid:
                continue

            produit = conso.get("produit") or {}
            id_produit = produit.get("idProduit")
            lot = conso.get("lot") or {}
            cont = conso.get("contenant") or {}
            contenance = float(cont.get("contenance", 0) or 0)
            lot_qty = int(lot.get("quantite", 0) or 0)

            if not (id_produit and contenance and lot_qty):
                continue

            fmt_str = f"{lot_qty}x{int(contenance * 100)}"

            try:
                detail = get_stock_produit_detail(sid)
                code_article = (detail.get("codeArticle") or "").strip()
                if code_article:
                    codes[(id_produit, fmt_str)] = code_article
            except (EasyBeerError, Exception):
                _log.warning("Erreur fetch detail stock %s", sid, exc_info=True)

            time.sleep(0.3)  # Rate limit (identique à fetch_carton_weights)

    _log.info("Codes articles récupérés : %d entrées", len(codes))
    return codes


# ─── Étape 6 : Assemblage ────────────────────────────────────────────────────


def _determine_brand(product_label: str) -> str:
    """Détermine la marque à partir du libellé produit.

    Si le nom contient 'niko' → NIKO, sinon SYMBIOSE.
    """
    if "niko" in product_label.lower():
        return "NIKO"
    return "SYMBIOSE"


def collect_label_data() -> list[dict[str, Any]]:
    """Point d'entrée principal : collecte tous les produits pour la sync étiquettes.

    Pour chaque brassin en cours :
      1. Calcule DDM et Lot
      2. Identifie le produit principal + dérivés
      3. Pour chaque produit × format, assemble les champs Access

    Retourne une liste de dicts prêts pour la table Access "Produits".
    """
    _log.info("=== Début collecte données étiquettes ===")

    # Étape 1 : Brassins en cours
    brassins = _fetch_active_brassins()
    if not brassins:
        _log.warning("Aucun brassin en cours, sync vide")
        return []

    # Étape 4 : Matrice codes-barres (appel unique, global)
    try:
        raw_matrice = get_code_barre_matrice()
        cb_by_product = _parse_barcode_matrix_for_labels(raw_matrice)
    except Exception:
        _log.exception("Erreur chargement matrice codes-barres")
        cb_by_product = {}

    # Étape 5 : Codes articles (appel unique, global)
    stock_codes = _fetch_stock_codes()

    # Étape 2+3+6 : Pour chaque brassin, assembler les produits
    products: list[dict[str, Any]] = []
    seen: set[str] = set()  # Dédoublonnage par (code_interne, lot)

    for brassin in brassins:
        id_brassin = brassin.get("idBrassin")
        brassin_produit = brassin.get("produit") or {}
        id_produit = brassin_produit.get("idProduit")
        brassin_nom = brassin.get("nom") or f"Brassin {id_brassin}"

        if not id_brassin or not id_produit:
            _log.warning("Brassin sans idBrassin ou idProduit, skip: %s", brassin_nom)
            continue

        # Détail brassin → DDM
        try:
            detail = get_brassin_detail(id_brassin)
        except Exception:
            _log.warning("Impossible de charger détail brassin %s, skip", id_brassin, exc_info=True)
            continue

        ddm = _compute_ddm(detail)
        lot = _ddm_to_lot(ddm)

        # Produit principal + dérivés
        all_product_ids = _get_all_product_ids(id_produit)
        _log.debug("Brassin %s : produit %s + %d dérivé(s)", brassin_nom, id_produit, len(all_product_ids) - 1)

        for pid in all_product_ids:
            # Récupérer le libellé du produit
            try:
                prod_detail = get_product_detail(pid)
                prod_label = (prod_detail.get("libelle") or "").strip()
            except Exception:
                _log.warning("Impossible de charger produit %s", pid, exc_info=True)
                continue

            if not prod_label:
                continue

            clean_label = clean_product_label(prod_label)
            marque = _determine_brand(prod_label)

            # Formats disponibles depuis la matrice codes-barres
            formats = cb_by_product.get(pid, {})
            if not formats:
                _log.debug("Produit %s (%s) : aucun format dans la matrice CB", pid, clean_label)
                continue

            for fmt_str, cb_info in formats.items():
                code_interne = stock_codes.get((pid, fmt_str), "")
                gtin_uvc = cb_info.get("gtin_uvc", "")
                gtin_colis = cb_info.get("gtin_colis", "")
                pcb = cb_info.get("pcb", 0)

                # Construire la désignation (ex: "Kéfir Gingembre — 12x33cl")
                designation = f"{clean_label} — {fmt_str}cl"

                # Dédoublonnage
                dedup_key = f"{code_interne or designation}|{lot}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                products.append({
                    "designation": designation,
                    "marque": marque,
                    "code_interne": code_interne,
                    "pcb": float(pcb),
                    "gtin_uvc": gtin_uvc,
                    "gtin_colis": gtin_colis,
                    "lot": float(lot),
                    "ddm": ddm.isoformat(),
                })

    _log.info(
        "=== Collecte terminée : %d produits pour %d brassins ===",
        len(products), len(brassins),
    )
    return products
