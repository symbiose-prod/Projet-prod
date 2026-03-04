"""
common/easybeer/products.py
===========================
Product, warehouse, and equipment endpoints with in-memory caching for rarely-changing data.
"""
from __future__ import annotations

import time as _time
from typing import Any

from ._client import BASE, TIMEOUT, _auth, _check_response, _safe_json, get_session, retry_api

# ─── In-memory cache for rarely-changing reference data ──────────────────────
_CACHE_TTL = 3600  # 1 heure (warehouses, materiels changent rarement)

_warehouses_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_materiels_cache: dict[str, Any] = {"data": None, "ts": 0.0}


def _cache_valid(cache: dict[str, Any]) -> bool:
    return cache["data"] is not None and (_time.monotonic() - cache["ts"]) < _CACHE_TTL


@retry_api
def get_all_products() -> list[dict[str, Any]]:
    """GET /parametres/produit/liste/all → Liste complete des produits."""
    ep = "parametres/produit/liste/all"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    return data if isinstance(data, list) else []


@retry_api
def get_warehouses() -> list[dict[str, Any]]:
    """GET /parametres/entrepot/liste → Liste de tous les entrepots (cache 1h)."""
    if _cache_valid(_warehouses_cache):
        return _warehouses_cache["data"]
    ep = "parametres/entrepot/liste"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    result = data if isinstance(data, list) else []
    if result:
        _warehouses_cache["data"] = result
        _warehouses_cache["ts"] = _time.monotonic()
    return result


@retry_api
def get_product_detail(id_produit: int) -> dict[str, Any]:
    """GET /parametres/produit/edition/{id} → Detail complet d'un produit."""
    ep = f"parametres/produit/edition/{id_produit}"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    return _safe_json(r, ep)


@retry_api
def get_all_materiels() -> list[dict[str, Any]]:
    """GET /parametres/materiel/liste/all → Liste complete du materiel (cache 1h)."""
    if _cache_valid(_materiels_cache):
        return _materiels_cache["data"]
    ep = "parametres/materiel/liste/all"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    result = data if isinstance(data, list) else []
    if result:
        _materiels_cache["data"] = result
        _materiels_cache["ts"] = _time.monotonic()
    return result
