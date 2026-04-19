"""
common/easybeer/products.py
===========================
Product, warehouse, and equipment endpoints with in-memory caching for rarely-changing data.

Pattern : chaque endpoint garde un cache L1 in-memory (spécifique par endpoint,
thread-safe via _cache_lock) et délègue L2 DB + appel HTTP au helper
:func:`common.easybeer.endpoint.execute_endpoint`. Le L1 reste hors du helper
car sa politique d'invalidation est métier (reset manuel via les fonctions
``invalidate_*_cache``, flush implicite au boot process).
"""
from __future__ import annotations

import threading as _threading
import time as _time
from typing import Any

from ._client import retry_api
from .endpoint import execute_endpoint

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
def get_all_products() -> list[dict[str, Any]]:
    """Liste complète des produits — L1 in-memory, L2 DB cache, L3 API."""
    # L1: in-memory
    with _cache_lock:
        if _cache_valid(_products_cache):
            return _products_cache["data"]
    # L2 + L3 via helper (gère cache_get + HTTP + cache_put + parsing défensif)
    data = execute_endpoint(
        method="GET",
        path="parametres/produit/liste/all",
        cache_key="products",
        cache_ttl=7200,
    )
    result = data if isinstance(data, list) else []
    if result:
        with _cache_lock:
            _products_cache["data"] = result
            _products_cache["ts"] = _time.monotonic()
    return result


def invalidate_products_cache() -> None:
    """Invalide le cache L1 liste produits (le cache L2 DB expire seul au TTL)."""
    with _cache_lock:
        _products_cache["data"] = None
        _products_cache["ts"] = 0.0


@retry_api
def get_warehouses() -> list[dict[str, Any]]:
    """Entrepôts — L1 in-memory, L2 DB cache, L3 API."""
    with _cache_lock:
        if _cache_valid(_warehouses_cache):
            return _warehouses_cache["data"]
    data = execute_endpoint(
        method="GET",
        path="parametres/entrepot/liste",
        cache_key="warehouses",
        cache_ttl=7200,
    )
    result = data if isinstance(data, list) else []
    if result:
        with _cache_lock:
            _warehouses_cache["data"] = result
            _warehouses_cache["ts"] = _time.monotonic()
    return result


@retry_api
def get_product_detail(id_produit: int) -> dict[str, Any]:
    """Détail produit — L1 in-memory (keyed), L2 DB cache, L3 API."""
    # L1: in-memory (par idProduit)
    now = _time.monotonic()
    with _cache_lock:
        cached = _product_detail_cache.get(id_produit)
        ts = _product_detail_ts.get(id_produit, 0)
    if cached is not None and (now - ts) < _PRODUCT_DETAIL_TTL:
        return cached
    # L2 + L3 via helper (cache_item_id permet une entrée DB par produit)
    data = execute_endpoint(
        method="GET",
        path=f"parametres/produit/edition/{id_produit}",
        cache_key="product_detail",
        cache_item_id=str(id_produit),
        cache_ttl=7200,
    )
    if data:
        with _cache_lock:
            _product_detail_cache[id_produit] = data
            _product_detail_ts[id_produit] = now
    return data


def invalidate_product_detail_cache(id_produit: int | None = None) -> None:
    """Invalide le cache L1 détail produit (un ou tous)."""
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
    with _cache_lock:
        if _cache_valid(_materiels_cache):
            return _materiels_cache["data"]
    data = execute_endpoint(
        method="GET",
        path="parametres/materiel/liste/all",
        cache_key="materiels",
        cache_ttl=7200,
    )
    result = data if isinstance(data, list) else []
    if result:
        with _cache_lock:
            _materiels_cache["data"] = result
            _materiels_cache["ts"] = _time.monotonic()
    return result
