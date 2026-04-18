"""
common/easybeer/products.py
===========================
Product, warehouse, and equipment endpoints with in-memory caching for rarely-changing data.
"""
from __future__ import annotations

import threading as _threading
import time as _time
from typing import Any

from ._client import BASE, TIMEOUT, _auth, _check_response, _safe_json, get_session, retry_api

# ─── In-memory cache for rarely-changing reference data ──────────────────────
_CACHE_TTL = 3600  # 1 heure (warehouses, materiels changent rarement)

_warehouses_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_materiels_cache: dict[str, Any] = {"data": None, "ts": 0.0}

# Cache détail produit (recette) — clé = idProduit, TTL 30 min
_PRODUCT_DETAIL_TTL = 1800
_product_detail_cache: dict[int, dict[str, Any]] = {}
_product_detail_ts: dict[int, float] = {}

# Thread-safe cache access. HTTP calls run OUTSIDE the lock to avoid
# serializing concurrent readers; only in-memory dict mutations are protected.
_cache_lock = _threading.Lock()


def _cache_valid(cache: dict[str, Any]) -> bool:
    return cache["data"] is not None and (_time.monotonic() - cache["ts"]) < _CACHE_TTL


_products_cache: dict[str, Any] = {"data": None, "ts": 0.0}


@retry_api
def _get_all_products_raw() -> list[dict[str, Any]]:
    """GET /parametres/produit/liste/all → Liste complete des produits (appel HTTP brut)."""
    ep = "parametres/produit/liste/all"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    return data if isinstance(data, list) else []


def get_all_products() -> list[dict[str, Any]]:
    """Liste complete des produits — L1 in-memory, L2 DB cache, L3 API."""
    # L1: in-memory
    with _cache_lock:
        if _cache_valid(_products_cache):
            return _products_cache["data"]
    # L2: DB cache
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        cached = cache_get(current_tenant_id(), "products", max_age_s=7200)
        if cached is not None:
            with _cache_lock:
                _products_cache["data"] = cached
                _products_cache["ts"] = _time.monotonic()
            return cached
    except Exception:
        pass
    # L3: API
    data = _get_all_products_raw()
    if data:
        with _cache_lock:
            _products_cache["data"] = data
            _products_cache["ts"] = _time.monotonic()
        try:
            from common._session import current_tenant_id
            from common.eb_cache import cache_put
            cache_put(current_tenant_id(), "products", data)
        except Exception:
            pass
    return data


def invalidate_products_cache() -> None:
    """Invalide le cache liste produits."""
    with _cache_lock:
        _products_cache["data"] = None
        _products_cache["ts"] = 0.0


@retry_api
def get_warehouses() -> list[dict[str, Any]]:
    """Entrepots — L1 in-memory, L2 DB cache, L3 API."""
    # L1
    with _cache_lock:
        if _cache_valid(_warehouses_cache):
            return _warehouses_cache["data"]
    # L2
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        cached = cache_get(current_tenant_id(), "warehouses", max_age_s=7200)
        if cached is not None:
            with _cache_lock:
                _warehouses_cache["data"] = cached
                _warehouses_cache["ts"] = _time.monotonic()
            return cached
    except Exception:
        pass
    # L3
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
        with _cache_lock:
            _warehouses_cache["data"] = result
            _warehouses_cache["ts"] = _time.monotonic()
        try:
            from common._session import current_tenant_id
            from common.eb_cache import cache_put
            cache_put(current_tenant_id(), "warehouses", result)
        except Exception:
            pass
    return result


@retry_api
def get_product_detail(id_produit: int) -> dict[str, Any]:
    """Detail produit — L1 in-memory, L2 DB cache, L3 API."""
    # L1: in-memory
    now = _time.monotonic()
    with _cache_lock:
        cached = _product_detail_cache.get(id_produit)
        ts = _product_detail_ts.get(id_produit, 0)
    if cached is not None and (now - ts) < _PRODUCT_DETAIL_TTL:
        return cached
    # L2: DB cache
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        db_cached = cache_get(current_tenant_id(), "product_detail", item_id=str(id_produit), max_age_s=7200)
        if db_cached is not None:
            with _cache_lock:
                _product_detail_cache[id_produit] = db_cached
                _product_detail_ts[id_produit] = now
            return db_cached
    except Exception:
        pass
    # L3: API
    ep = f"parametres/produit/edition/{id_produit}"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    if data:
        with _cache_lock:
            _product_detail_cache[id_produit] = data
            _product_detail_ts[id_produit] = now
        try:
            from common._session import current_tenant_id
            from common.eb_cache import cache_put
            cache_put(current_tenant_id(), "product_detail", data, item_id=str(id_produit))
        except Exception:
            pass
    return data


def invalidate_product_detail_cache(id_produit: int | None = None) -> None:
    """Invalide le cache détail produit (un ou tous)."""
    with _cache_lock:
        if id_produit is not None:
            _product_detail_cache.pop(id_produit, None)
            _product_detail_ts.pop(id_produit, None)
        else:
            _product_detail_cache.clear()
            _product_detail_ts.clear()


@retry_api
def get_all_materiels() -> list[dict[str, Any]]:
    """Matériel — L1 in-memory, L2 DB cache, L3 API."""
    # L1
    with _cache_lock:
        if _cache_valid(_materiels_cache):
            return _materiels_cache["data"]
    # L2
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        cached = cache_get(current_tenant_id(), "materiels", max_age_s=7200)
        if cached is not None:
            with _cache_lock:
                _materiels_cache["data"] = cached
                _materiels_cache["ts"] = _time.monotonic()
            return cached
    except Exception:
        pass
    # L3
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
        with _cache_lock:
            _materiels_cache["data"] = result
            _materiels_cache["ts"] = _time.monotonic()
        try:
            from common._session import current_tenant_id
            from common.eb_cache import cache_put
            cache_put(current_tenant_id(), "materiels", result)
        except Exception:
            pass
    return result
