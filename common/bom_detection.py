"""
common/bom_detection.py
=======================
Auto-detection of BOM entries from EasyBeer data.

Fetches the finished-product stock list (POST /stock/produits), then for
each product-format calls GET /stock/produit/edition/{id} to read the
conditioning elements (étiquettes, capsules, cartons) configured in EasyBeer.

Detected entries are stored with ``validated=False`` so the user can
review and confirm them on the /nomenclatures page.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

_log = logging.getLogger("ferment.bom_detection")


# ─── Fetch stock produits (finished products with formats) ────────────────

def _fetch_stock_produits() -> dict[str, Any]:
    """POST /stock/produits → all finished-product stock consolidations."""
    from common.easybeer._client import BASE, _auth, _check_response, _safe_json, get_session

    id_brasserie = int(os.environ.get("EASYBEER_ID_BRASSERIE", "0"))
    r = get_session().post(
        f"{BASE}/stock/produits",
        json={"idBrasserie": id_brasserie},
        auth=_auth(),
        timeout=30,
    )
    _check_response(r, "stock/produits")
    return _safe_json(r, "stock/produits")


def _build_stock_map(
    produits_data: dict[str, Any],
) -> dict[tuple[int, str], dict[str, Any]]:
    """Build (idProduit, format_code) → {sid, libelle, contenance, lot_qty}.

    Parses the consolidation tree from POST /stock/produits.
    """
    from common.easybeer.products import get_all_products

    # idProduit → libelle lookup
    id_to_label: dict[int, str] = {}
    for p in get_all_products():
        pid = p.get("idProduit")
        lib = (p.get("libelle") or "").strip()
        if pid and lib:
            id_to_label[pid] = lib

    stock_map: dict[tuple[int, str], dict[str, Any]] = {}
    for prod in produits_data.get("consolidationsFilles", []):
        for conso in prod.get("consolidationsFilles", []):
            sid = conso.get("id")
            if not sid:
                continue
            produit = conso.get("produit") or {}
            id_produit = produit.get("idProduit")
            cont = conso.get("contenant") or {}
            contenance = float(cont.get("contenance", 0) or 0)
            lot = conso.get("lot") or {}
            lot_qty = int(lot.get("quantite", 0) or 0)
            if id_produit and contenance and lot_qty:
                fmt_str = f"{lot_qty}x{int(contenance * 100)}"
                stock_map[(id_produit, fmt_str)] = {
                    "sid": sid,
                    "libelle": id_to_label.get(id_produit, f"Produit #{id_produit}"),
                    "contenance": contenance,
                    "lot_qty": lot_qty,
                }

    return stock_map


# ─── Read conditioning elements from EasyBeer ─────────────────────────────

def _detect_from_stock_detail(
    id_produit: int,
    product_label: str,
    format_code: str,
    lot_qty: int,
    id_stock_produit: int,
) -> list[dict[str, Any]] | None:
    """Fetch GET /stock/produit/edition/{id} and extract conditioning elements.

    Returns BOM entry dicts ready for ``bulk_upsert_bom()``,
    or ``None`` if rate-limited (caller should stop).
    """
    from common.easybeer.stocks import get_stock_produit_detail

    try:
        detail = get_stock_produit_detail(id_stock_produit)
    except Exception as exc:
        _log.warning(
            "Cannot fetch stock detail %d for %s %s: %s",
            id_stock_produit, product_label, format_code, exc,
        )
        # Rate-limit → signal caller to stop
        if "rate-limit" in str(exc).lower() or "banned" in str(exc).lower():
            return None
        return []

    entries: list[dict[str, Any]] = []
    for elem in detail.get("elementsConditionnement") or []:
        mp = elem.get("elementMatierePremiere") or {}
        id_mp = mp.get("idMatierePremiere")
        if id_mp is None:
            continue
        qty = float(elem.get("quantite", 0) or 0)
        if qty <= 0:
            continue
        mp_label = (mp.get("libelle") or "").strip()

        entries.append({
            "id_produit": id_produit,
            "format_code": format_code,
            "product_label": product_label,
            "id_mp": id_mp,
            "mp_label": mp_label,
            "qty_per_unit": qty,
            "validated": False,
            "source": "auto_detected",
        })
        _log.info(
            "EasyBeer BOM: %s %s → %s (qty=%.0f)",
            product_label, format_code, mp_label, qty,
        )

    return entries


# ─── Product formats (still useful for the nomenclatures page) ────────────

def detect_product_formats_from_stocks(
    produits_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build product-format list from POST /stock/produits data.

    Returns::

        [
            {
                "id_produit": 42,
                "libelle": "Kéfir Gingembre",
                "formats": [
                    {"format_code": "12x33", "contenance": 0.33, "lot_qty": 12},
                ]
            },
            ...
        ]
    """
    if produits_data is None:
        produits_data = _fetch_stock_produits()

    stock_map = _build_stock_map(produits_data)

    # Group by id_produit
    by_product: dict[int, dict[str, Any]] = {}
    for (pid, fmt), info in stock_map.items():
        if pid not in by_product:
            by_product[pid] = {
                "id_produit": pid,
                "libelle": info["libelle"],
                "formats": {},
            }
        by_product[pid]["formats"][fmt] = {
            "format_code": fmt,
            "contenance": info["contenance"],
            "lot_qty": info["lot_qty"],
        }

    result: list[dict[str, Any]] = []
    for pid, data in sorted(by_product.items()):
        result.append({
            "id_produit": pid,
            "libelle": data["libelle"],
            "formats": sorted(data["formats"].values(), key=lambda f: f["format_code"]),
        })

    result.sort(key=lambda r: r["libelle"])
    return result


# ─── Full detection orchestrator ───────────────────────────────────────────

def run_full_detection(tenant_id: str | None = None) -> tuple[int, int]:
    """Run full BOM auto-detection from EasyBeer stock data.

    1. Fetch finished-product stock list (POST /stock/produits)
    2. For each product-format, fetch conditioning elements via stock detail
    3. Bulk upsert into DB (without overwriting validated or conditioning entries)

    Returns ``(total_detected, products_detected)``.
    """
    from common.product_bom import bulk_upsert_bom

    _log.info("Starting full BOM detection from EasyBeer...")

    # 1. Fetch all finished-product stocks
    produits_data = _fetch_stock_produits()
    stock_map = _build_stock_map(produits_data)
    _log.info("Found %d product-formats in EasyBeer stock", len(stock_map))

    # 2. For each product-format, fetch conditioning elements
    all_entries: list[dict[str, Any]] = []
    products_seen: set[int] = set()

    from common.easybeer._client import is_rate_limited

    for (id_produit, fmt), info in sorted(stock_map.items()):
        # Check rate-limit before each API call
        if is_rate_limited() > 0:
            _log.warning("Rate-limit actif, arrêt détection BOM (%d entries, %d produits)", len(all_entries), len(products_seen))
            break

        entries = _detect_from_stock_detail(
            id_produit=id_produit,
            product_label=info["libelle"],
            format_code=fmt,
            lot_qty=info["lot_qty"],
            id_stock_produit=info["sid"],
        )
        if entries:
            all_entries.extend(entries)
            products_seen.add(id_produit)
        elif entries is None:
            # Rate-limited — stop fetching, save what we have
            _log.warning("Rate-limited, stopping BOM detection early")
            break

    # 2b. Auto-detect bottles (CONTENANT) from format codes
    #     12x33 → 12 × Bouteille 33cl, 6x75 → 6 × Bouteille 75cl
    bottle_entries = _detect_bottles_from_formats(stock_map)
    if bottle_entries:
        all_entries.extend(bottle_entries)
        for be in bottle_entries:
            products_seen.add(be["id_produit"])

    # 3. Bulk upsert (respects existing validated/conditioning entries)
    if all_entries:
        bulk_upsert_bom(all_entries, tenant_id=tenant_id)

    _log.info(
        "BOM detection complete: %d entries for %d products",
        len(all_entries), len(products_seen),
    )
    return len(all_entries), len(products_seen)


def _detect_bottles_from_formats(
    stock_map: dict[tuple[int, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Auto-detect bottle (CONTENANT) BOM entries from product formats.

    For each product-format, derives the bottle type from contenance:
    - contenance 0.33 → matches MP with "bouteille" + "33" in label
    - contenance 0.75 → matches MP with "bouteille" + "75" in label

    The quantity is the lot_qty (bottles per carton).
    """
    import re
    from common.easybeer.stocks import get_all_matieres_premieres

    all_mp = get_all_matieres_premieres()

    # Build bottle lookup: contenance_cl → {id_mp, label}
    # Bottles are CONTENANT type MP with "bouteille" in the label
    bottles_by_cl: dict[int, dict[str, Any]] = {}
    for mp in all_mp:
        mp_type = (mp.get("type") or {}).get("code", "")
        label = (mp.get("libelle") or "").strip()
        mp_id = mp.get("idMatierePremiere")
        if mp_type != "CONTENANT" or not mp_id:
            continue
        label_lower = label.lower()
        if "bouteille" not in label_lower and "eau" not in label_lower:
            continue
        # Extract cl from label: "Bouteille 33cl" → 33, "EAU GAZEUSE 75cl" → 75
        m = re.search(r"(\d+)\s*cl", label_lower)
        if m:
            cl = int(m.group(1))
            bottles_by_cl[cl] = {"id_mp": mp_id, "label": label}
        else:
            # Try contenance field
            cont = float(mp.get("contenance", 0) or 0)
            if cont > 0:
                cl = int(cont * 100)
                bottles_by_cl[cl] = {"id_mp": mp_id, "label": label}

    if not bottles_by_cl:
        _log.warning("Aucune bouteille CONTENANT trouvée dans les MP EasyBeer")
        return []

    _log.info("Bouteilles détectées: %s", {cl: b["label"] for cl, b in bottles_by_cl.items()})

    entries: list[dict[str, Any]] = []
    for (id_produit, fmt), info in stock_map.items():
        contenance_cl = int(info["contenance"] * 100)
        bottle = bottles_by_cl.get(contenance_cl)
        if not bottle:
            _log.debug("Pas de bouteille %dcl pour %s %s", contenance_cl, info["libelle"], fmt)
            continue

        qty = info["lot_qty"]  # bottles per carton = lot_qty
        entries.append({
            "id_produit": id_produit,
            "format_code": fmt,
            "product_label": info["libelle"],
            "id_mp": bottle["id_mp"],
            "mp_label": bottle["label"],
            "qty_per_unit": qty,
            "validated": False,
            "source": "auto_detected",
        })
        _log.info(
            "BOM bouteille: %s %s → %s (qty=%d)",
            info["libelle"], fmt, bottle["label"], qty,
        )

    return entries
