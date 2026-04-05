"""
common/eb_sync_loop.py
======================
Background sync loop that periodically fetches EasyBeer API data into the
persistent ``eb_cache`` PostgreSQL table.

Registered at startup in ``app_nicegui.py`` via::

    asyncio.ensure_future(eb_cache_sync_loop())

Each category has its own sync interval.  The loop wakes every 60 s, checks
which categories are stale, and syncs them one at a time (respecting the
1 req/s rate-limit).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from collections.abc import Callable

_log = logging.getLogger("ferment.eb_sync")


# ─── Category registry ────────────────────────────────────────────────────

# (cache_key, interval_seconds, sync_function_name)
# Functions are resolved lazily to avoid circular imports.
_CATEGORIES: list[tuple[str, int, str]] = [
    # Volatile — sync often
    ("brassins_en_cours",  300, "_sync_brassins_en_cours"),
    ("brassins_planifies", 600, "_sync_brassins_planifies"),
    ("autonomie_stocks",   900, "_sync_autonomie_stocks"),
    # Reference — sync hourly
    ("products",          3600, "_sync_products"),
    ("mp_all",            3600, "_sync_mp_all"),
    ("bottle_stock",      3600, "_sync_bottle_stock"),
    ("fournisseurs",      3600, "_sync_fournisseurs"),
    ("warehouses",        3600, "_sync_warehouses"),
    ("materiels",         3600, "_sync_materiels"),
    ("mp_historique",     3600, "_sync_mp_historique"),
    ("brassins_archives", 3600, "_sync_brassins_archives"),
    ("code_barre_matrice", 86400, "_sync_code_barre_matrice"),
    ("carton_weights",   86400, "_sync_carton_weights"),
]

_TICK_INTERVAL = 60  # seconds between wake-ups


# ─── Individual sync functions (blocking — called via asyncio.to_thread) ──

def _sync_brassins_en_cours(tenant_id: str) -> tuple[int, str | None]:
    """Sync active brews."""
    from common.easybeer.brassins import get_brassins_en_cours
    from common.eb_cache import cache_put

    data = get_brassins_en_cours()
    cache_put(tenant_id, "brassins_en_cours", data)
    return len(data), None


def _sync_brassins_planifies(tenant_id: str) -> tuple[int, str | None]:
    """Sync planned brews (90-day horizon)."""
    from common.easybeer.brassins import get_brassins_planifies
    from common.eb_cache import cache_put

    data = get_brassins_planifies(days_ahead=90)
    cache_put(tenant_id, "brassins_planifies", data)
    return len(data), None


_AUTONOMIE_WINDOWS = [30, 60, 90, 180, 365]


def _sync_autonomie_stocks(tenant_id: str) -> tuple[int, str | None]:
    """Sync stock autonomy for all standard periods (30, 60, 90, 180, 365 days).

    Appelle l'API directement (bypass L2 cache) pour chaque période.
    """
    from common.easybeer._client import (
        BASE,
        TIMEOUT,
        _auth,
        _check_response,
        _indicator_payload,
        _safe_json,
        get_session,
        is_rate_limited,
    )
    from common.eb_cache import cache_put

    count = 0
    for days in _AUTONOMIE_WINDOWS:
        if is_rate_limited() > 0:
            _log.warning("Rate-limit during autonomie sync, stopping at %dj", days)
            break
        try:
            r = get_session().post(
                f"{BASE}/indicateur/autonomie-stocks",
                params={"forceRefresh": False},
                json=_indicator_payload(days),
                auth=_auth(),
                timeout=TIMEOUT,
            )
            _check_response(r, f"autonomie-stocks/{days}j")
            data = _safe_json(r, f"autonomie-stocks/{days}j")
            cache_put(tenant_id, "autonomie_stocks", data, item_id=str(days))
            count += 1
        except Exception:
            _log.warning("Autonomie sync %dj failed", days, exc_info=True)
    return count, None


def _sync_products(tenant_id: str) -> tuple[int, str | None]:
    """Sync product list + individual product details.

    Uses raw API calls to avoid polluting the in-memory cache with
    all product details (which would grow indefinitely).
    """
    from common.easybeer._client import (
        BASE,
        TIMEOUT,
        _auth,
        _check_response,
        _safe_json,
        get_session,
        is_rate_limited,
    )
    from common.easybeer.products import _get_all_products_raw
    from common.eb_cache import cache_put

    products = _get_all_products_raw()
    cache_put(tenant_id, "products", products)

    # Sync individual product details (recipes) via raw API calls
    # to avoid filling _product_detail_cache in-memory indefinitely.
    count = len(products)
    for p in products:
        if is_rate_limited() > 0:
            _log.warning("Rate-limit during product_detail sync, stopping early")
            break
        pid = p.get("idProduit")
        if not pid:
            continue
        try:
            ep = f"parametres/produit/edition/{pid}"
            r = get_session().get(f"{BASE}/{ep}", auth=_auth(), timeout=TIMEOUT)
            _check_response(r, ep)
            detail = _safe_json(r, ep)
            if detail:
                cache_put(tenant_id, "product_detail", detail, item_id=str(pid))
        except Exception:
            _log.debug("Skip product_detail %d", pid, exc_info=True)

    return count, None


def _sync_bottle_stock(tenant_id: str) -> tuple[int, str | None]:
    """Sync bottle (contenant) stock."""
    from common.easybeer.stocks import _BOTTLE_STOCK_CACHE, get_bottle_stock
    from common.eb_cache import cache_put
    _BOTTLE_STOCK_CACHE["data"] = None  # force refresh
    data = get_bottle_stock()
    cache_put(tenant_id, "bottle_stock", data)
    return len(data), None


def _sync_mp_all(tenant_id: str) -> tuple[int, str | None]:
    """Sync all matières premières."""
    # Bypass in-memory cache by calling the API directly
    from common.easybeer.stocks import _MP_CACHE, get_all_matieres_premieres
    from common.eb_cache import cache_put
    _MP_CACHE["data"] = None  # force refresh
    data = get_all_matieres_premieres()
    cache_put(tenant_id, "mp_all", data)
    return len(data), None


def _sync_fournisseurs(tenant_id: str) -> tuple[int, str | None]:
    """Sync suppliers."""
    from common.easybeer.suppliers import _get_all_fournisseurs_raw
    from common.eb_cache import cache_put

    data = _get_all_fournisseurs_raw()
    cache_put(tenant_id, "fournisseurs", data)
    return len(data), None


def _sync_warehouses(tenant_id: str) -> tuple[int, str | None]:
    """Sync warehouses."""
    # Force refresh by clearing the in-memory cache
    from common.easybeer.products import _warehouses_cache, get_warehouses
    from common.eb_cache import cache_put
    _warehouses_cache["data"] = None
    data = get_warehouses()
    cache_put(tenant_id, "warehouses", data)
    return len(data), None


def _sync_materiels(tenant_id: str) -> tuple[int, str | None]:
    """Sync equipment."""
    from common.easybeer.products import _materiels_cache, get_all_materiels
    from common.eb_cache import cache_put
    _materiels_cache["data"] = None
    data = get_all_materiels()
    cache_put(tenant_id, "materiels", data)
    return len(data), None


def _sync_mp_historique(tenant_id: str) -> tuple[int, str | None]:
    """Sync MP entry history for each category."""
    from common.easybeer._client import is_rate_limited
    from common.easybeer.history import get_mp_historique_entree, invalidate_mp_historique_cache
    from common.eb_cache import cache_put

    total = 0
    for cat in ("Conditionnement", "Ingredient", "Divers"):
        if is_rate_limited() > 0:
            _log.warning("Rate-limit during mp_historique sync, stopping at %s", cat)
            break
        invalidate_mp_historique_cache(cat)
        data = get_mp_historique_entree(cat)
        cache_put(tenant_id, "mp_historique", data, item_id=cat)
        total += len(data)
    return total, None


def _sync_brassins_archives(tenant_id: str) -> tuple[int, str | None]:
    """Sync archived brews (3 most recent, 60-day window)."""
    from common.easybeer.brassins import get_brassins_archives
    from common.eb_cache import cache_put
    data = get_brassins_archives(nombre=3, jours=60)
    cache_put(tenant_id, "brassins_archives", data, item_id="3_60")
    return len(data), None


def _sync_code_barre_matrice(tenant_id: str) -> tuple[int, str | None]:
    """Sync barcode matrix (rarely changes)."""
    from common.easybeer.conditioning import get_code_barre_matrice
    from common.eb_cache import cache_put
    data = get_code_barre_matrice()
    cache_put(tenant_id, "code_barre_matrice", data)
    return 1, None


def _sync_carton_weights(tenant_id: str) -> tuple[int, str | None]:
    """Sync carton weights (heavy operation, 24h cache)."""
    from common.easybeer.stocks import fetch_carton_weights
    from common.eb_cache import cache_put
    weights = fetch_carton_weights()
    db_data = [{"pid": pid, "fmt": fmt, "w": w} for (pid, fmt), w in weights.items()]
    cache_put(tenant_id, "carton_weights", db_data)
    return len(weights), None


# ─── Sync dispatcher ──────────────────────────────────────────────────────

# Map function names to actual callables
_SYNC_FNS: dict[str, Callable[[str], tuple[int, str | None]]] = {
    "_sync_brassins_en_cours": _sync_brassins_en_cours,
    "_sync_brassins_planifies": _sync_brassins_planifies,
    "_sync_autonomie_stocks": _sync_autonomie_stocks,
    "_sync_products": _sync_products,
    "_sync_mp_all": _sync_mp_all,
    "_sync_bottle_stock": _sync_bottle_stock,
    "_sync_fournisseurs": _sync_fournisseurs,
    "_sync_warehouses": _sync_warehouses,
    "_sync_materiels": _sync_materiels,
    "_sync_mp_historique": _sync_mp_historique,
    "_sync_brassins_archives": _sync_brassins_archives,
    "_sync_code_barre_matrice": _sync_code_barre_matrice,
    "_sync_carton_weights": _sync_carton_weights,
}


def _resolve_tenant_id() -> str | None:
    """Resolve the production tenant ID from ALLOWED_TENANTS env var."""
    tenant_name = os.environ.get("ALLOWED_TENANTS", "").split(",")[0].strip()
    if not tenant_name:
        return None
    try:
        from common.auth import ensure_tenant_id
        return ensure_tenant_id(tenant_name)
    except Exception:
        _log.debug("Cannot resolve tenant '%s'", tenant_name, exc_info=True)
        return None


def _run_sync_tick(tenant_id: str) -> None:
    """One sync tick: check each category and sync if stale."""
    from common.easybeer._client import is_rate_limited
    from common.eb_cache import needs_sync, sync_meta_update

    for cache_key, interval_s, fn_name in _CATEGORIES:
        # Bail out if rate-limited
        if is_rate_limited() > 0:
            _log.debug("Rate-limit active, pausing sync tick")
            return

        if not needs_sync(tenant_id, cache_key, interval_s):
            continue

        fn = _SYNC_FNS[fn_name]
        t0 = _time.monotonic()
        try:
            item_count, error_msg = fn(tenant_id)
            duration = _time.monotonic() - t0
            sync_meta_update(
                tenant_id, cache_key,
                duration_s=duration,
                item_count=item_count,
            )
            _log.info(
                "EB sync %-25s : %d items in %.1fs",
                cache_key, item_count, duration,
            )
        except Exception as exc:
            duration = _time.monotonic() - t0
            _log.warning("EB sync %-25s : FAILED (%.1fs) %s", cache_key, duration, exc)
            sync_meta_update(
                tenant_id, cache_key,
                duration_s=duration,
                item_count=0,
                error_count=1,
                last_error=str(exc)[:500],
            )


# ─── Main async loop ──────────────────────────────────────────────────────

async def eb_cache_sync_loop() -> None:
    """Infinite async loop — register with ``asyncio.ensure_future()`` at startup."""
    from common.easybeer import is_configured

    _log.info("EasyBeer cache sync loop started (tick=%ds)", _TICK_INTERVAL)

    # Wait a bit for the app to fully start
    await asyncio.sleep(10)

    while True:
        try:
            if not is_configured():
                await asyncio.sleep(_TICK_INTERVAL)
                continue

            tenant_id = await asyncio.to_thread(_resolve_tenant_id)
            if not tenant_id:
                await asyncio.sleep(_TICK_INTERVAL)
                continue

            await asyncio.to_thread(_run_sync_tick, tenant_id)

        except Exception:
            _log.exception("Error in EasyBeer cache sync loop")

        await asyncio.sleep(_TICK_INTERVAL)
