"""
ui/_stock_pf_calc.py
====================
Logique de calcul pour la page Stock produits finis.

Compare le stock EasyBeer (API) avec le stock Sofripa (CSV upload).
Thread-safe, pas de dépendance NiceGUI.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
from typing import Any

_log = logging.getLogger("ferment.stock_pf")


# ─── Parsing CSV Sofripa ──────────────────────────────────────────────────────

# Références non-produit à ignorer
_SKIP_REFS = {"BOUTEILLEVIDE33", "BOUTEILLEVIDE75", "ECHAN"}


def _clean_qty(raw: str) -> int:
    """Nettoie une quantité Sofripa : '1 007' → 1007, vide → 0."""
    s = str(raw or "").strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        return 0


def parse_sofripa_csv(csv_content: str) -> dict[str, dict[str, Any]]:
    """Parse le CSV ARTICLES.csv de Sofripa.

    Retourne : {ref6: {designation, qty, en_prepa, en_recept}}
    """
    result: dict[str, dict[str, Any]] = {}
    reader = csv.DictReader(io.StringIO(csv_content))
    for row in reader:
        ref = str(row.get("REFERENCE", "")).strip()
        if not ref or ref in _SKIP_REFS:
            continue
        result[ref] = {
            "designation": str(row.get("DESIGNATION", "")).strip(),
            "qty": _clean_qty(row.get("QTE_ARTICLE", "")),
            "en_prepa": _clean_qty(row.get("QTE_ART_EN_PREPA", "")),
            "en_recept": _clean_qty(row.get("QTE_ARTICLE_ENRECEPT", "")),
        }
    return result


# ─── Chargement EasyBeer ───────────────────────────────────────────────────────

def _fetch_eb_stock_by_ref(
    cb_index: dict[str, dict],
) -> dict[str, dict[str, Any]]:
    """Appelle POST /stock/produits et indexe par ref6 via la matrice codes-barres.

    Retourne : {ref6: {produit_label, quantiteReelle, quantiteVirtuelle, fmt_str}}
    """
    from common.easybeer._client import (
        BASE, TIMEOUT, _auth, _check_response, _safe_json, get_session,
    )

    payload = {"idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "0"))}
    r = get_session().post(
        f"{BASE}/stock/produits",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "stock/produits")
    data = _safe_json(r, "stock/produits")

    # Index inverse : (idProduit, fmt_str) → ref6
    id_fmt_to_ref: dict[tuple[int, str], str] = {}
    for ref6, info in cb_index.items():
        id_fmt_to_ref[(info["idProduit"], info["fmt_str"])] = ref6

    result: dict[str, dict[str, Any]] = {}

    for prod_node in data.get("consolidationsFilles", []):
        for conso_node in prod_node.get("consolidationsFilles", []):
            produit = conso_node.get("produit") or {}
            id_produit = produit.get("idProduit")
            produit_label = produit.get("libelle") or ""
            cont = conso_node.get("contenant") or {}
            contenance = float(cont.get("contenance", 0) or 0)
            lot = conso_node.get("lot") or {}
            lot_qty = int(lot.get("quantite", 0) or 0)

            if not (id_produit and contenance and lot_qty):
                continue

            fmt_str = f"{lot_qty}x{int(contenance * 100)}"
            ref6 = id_fmt_to_ref.get((id_produit, fmt_str))
            if not ref6:
                continue

            # Accumuler les quantités (un même ref6 peut avoir plusieurs lots)
            q_reelle = int(conso_node.get("quantiteReelle", 0) or 0)
            q_virtuelle = int(conso_node.get("quantiteVirtuelle", 0) or 0)

            if ref6 in result:
                result[ref6]["quantiteReelle"] += q_reelle
                result[ref6]["quantiteVirtuelle"] += q_virtuelle
            else:
                result[ref6] = {
                    "produit_label": produit_label,
                    "quantiteReelle": q_reelle,
                    "quantiteVirtuelle": q_virtuelle,
                    "fmt_str": fmt_str,
                }

    return result


def _build_cb_index() -> dict[str, dict[str, Any]]:
    """Construit un index {ref6: {idProduit, fmt_str, produit_label}} depuis la matrice codes-barres.

    Réutilise get_code_barre_matrice() + parse_barcode_matrix() existants.
    """
    from common.easybeer.conditioning import get_code_barre_matrice
    from common.easybeer.products import get_all_products
    from common.ramasse import parse_barcode_matrix

    # Matrice codes-barres → {idProduit: [{ref6, fmt_str, ...}]}
    raw_matrice = get_code_barre_matrice()
    by_product = parse_barcode_matrix(raw_matrice)

    # Produits → {idProduit: libelle}
    products = get_all_products()
    prod_labels: dict[int, str] = {}
    for p in products:
        pid = p.get("idProduit")
        if pid:
            prod_labels[pid] = p.get("libelle", "")

    # Index par ref6
    index: dict[str, dict[str, Any]] = {}
    for id_produit, entries in by_product.items():
        for entry in entries:
            ref6 = entry["ref6"]
            index[ref6] = {
                "idProduit": id_produit,
                "fmt_str": entry["fmt_str"],
                "produit_label": prod_labels.get(id_produit, ""),
            }

    return index


# ─── Fonction principale ───────────────────────────────────────────────────────

def fetch_stock_comparison(csv_content: str) -> dict[str, Any]:
    """Compare le stock EasyBeer avec le stock Sofripa (CSV).

    Retourne :
        {
            "rows": [{ref, designation, stock_eb_reel, stock_eb_virtuel,
                       en_cours_eb, stock_sofripa, en_prepa, en_recept, ecart}],
            "summary": {total_eb_reel, total_sofripa, total_ecart,
                        nb_produits, nb_ecarts},
            "unmatched_csv": [ref6 du CSV sans correspondance EasyBeer],
            "unmatched_eb": [ref6 EasyBeer sans correspondance CSV],
        }
    """
    # 1. Parser le CSV
    sofripa = parse_sofripa_csv(csv_content)
    _log.info("CSV Sofripa parsé : %d produits", len(sofripa))

    # 2. Construire l'index codes-barres
    cb_index = _build_cb_index()
    _log.info("Index codes-barres : %d refs", len(cb_index))

    # 3. Charger stock EasyBeer
    eb_stock = _fetch_eb_stock_by_ref(cb_index)
    _log.info("Stock EasyBeer : %d refs", len(eb_stock))

    # 4. Matcher et construire les lignes
    rows: list[dict[str, Any]] = []
    all_refs = set(sofripa.keys()) | set(eb_stock.keys())
    unmatched_csv: list[str] = []
    unmatched_eb: list[str] = []

    for ref in sorted(all_refs):
        csv_data = sofripa.get(ref)
        eb_data = eb_stock.get(ref)

        if csv_data and not eb_data:
            # Produit dans CSV mais pas dans EasyBeer
            unmatched_csv.append(ref)
            rows.append({
                "ref": ref,
                "designation": csv_data["designation"],
                "stock_eb_reel": 0,
                "stock_eb_virtuel": 0,
                "en_cours_eb": 0,
                "stock_sofripa": csv_data["qty"],
                "en_prepa": csv_data["en_prepa"],
                "en_recept": csv_data["en_recept"],
                "ecart": csv_data["qty"],
                "match": False,
            })
            continue

        if eb_data and not csv_data:
            # Produit dans EasyBeer mais pas dans CSV
            unmatched_eb.append(ref)
            q_reel = eb_data["quantiteReelle"]
            q_virt = eb_data["quantiteVirtuelle"]
            rows.append({
                "ref": ref,
                "designation": eb_data.get("produit_label", ref),
                "stock_eb_reel": q_reel,
                "stock_eb_virtuel": q_virt,
                "en_cours_eb": q_reel - q_virt,
                "stock_sofripa": 0,
                "en_prepa": 0,
                "en_recept": 0,
                "ecart": -q_reel,
                "match": False,
            })
            continue

        # Produit dans les deux
        q_reel = eb_data["quantiteReelle"]
        q_virt = eb_data["quantiteVirtuelle"]
        en_cours = q_reel - q_virt
        stock_sofripa = csv_data["qty"]
        ecart = stock_sofripa - q_reel

        rows.append({
            "ref": ref,
            "designation": csv_data["designation"],
            "stock_eb_reel": q_reel,
            "stock_eb_virtuel": q_virt,
            "en_cours_eb": en_cours,
            "stock_sofripa": stock_sofripa,
            "en_prepa": csv_data["en_prepa"],
            "en_recept": csv_data["en_recept"],
            "ecart": ecart,
            "match": True,
        })

    # Tri : écarts les plus importants en premier (valeur absolue)
    rows.sort(key=lambda r: -abs(r["ecart"]))

    # Summary
    total_eb = sum(r["stock_eb_reel"] for r in rows)
    total_sofripa = sum(r["stock_sofripa"] for r in rows)
    total_ecart = sum(r["ecart"] for r in rows)
    nb_ecarts = sum(1 for r in rows if r["ecart"] != 0)

    return {
        "rows": rows,
        "summary": {
            "total_eb_reel": total_eb,
            "total_sofripa": total_sofripa,
            "total_ecart": total_ecart,
            "nb_produits": len(rows),
            "nb_ecarts": nb_ecarts,
        },
        "unmatched_csv": unmatched_csv,
        "unmatched_eb": unmatched_eb,
    }
