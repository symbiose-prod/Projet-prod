"""
common/eb_cache.py
==================
Persistent EasyBeer API cache backed by PostgreSQL JSONB.

Provides a generic key/value store (table ``eb_cache``) and sync metadata
tracking (table ``eb_sync_meta``) for the background sync loop.

Usage::

    from common.eb_cache import cache_get, cache_put

    data = cache_get(tenant_id, "products", max_age_s=3600)
    if data is None:
        data = fetch_from_api()
        cache_put(tenant_id, "products", data)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from db.conn import run_sql

_log = logging.getLogger("ferment.eb_cache")


# ─── Read ──────────────────────────────────────────────────────────────────

def cache_get(
    tenant_id: str,
    cache_key: str,
    item_id: str = "",
    max_age_s: int = 3600,
) -> Any | None:
    """Read a cached value if it exists and is younger than *max_age_s*.

    Returns the deserialized JSONB data, or ``None`` if not found / expired.
    """
    rows = run_sql(
        """
        SELECT data
        FROM eb_cache
        WHERE tenant_id = :t
          AND cache_key = :k
          AND item_id   = :i
          AND fetched_at > now() - make_interval(secs => :age)
        LIMIT 1
        """,
        {"t": tenant_id, "k": cache_key, "i": item_id, "age": max_age_s},
    )
    if rows:
        return rows[0]["data"]
    return None


# ─── Write ─────────────────────────────────────────────────────────────────

def cache_put(
    tenant_id: str,
    cache_key: str,
    data: Any,
    item_id: str = "",
) -> None:
    """Upsert a value into the cache."""
    run_sql(
        """
        INSERT INTO eb_cache (tenant_id, cache_key, item_id, data, fetched_at)
        VALUES (:t, :k, :i, :d::jsonb, now())
        ON CONFLICT (tenant_id, cache_key, item_id) DO UPDATE
        SET data       = :d::jsonb,
            fetched_at = now()
        """,
        {
            "t": tenant_id,
            "k": cache_key,
            "i": item_id,
            "d": json.dumps(data, default=str, ensure_ascii=False),
        },
    )


def cache_delete(
    tenant_id: str,
    cache_key: str,
    item_id: str | None = None,
) -> int:
    """Delete cache entries.  If *item_id* is ``None``, deletes all items for the key."""
    if item_id is not None:
        return run_sql(
            "DELETE FROM eb_cache WHERE tenant_id = :t AND cache_key = :k AND item_id = :i",
            {"t": tenant_id, "k": cache_key, "i": item_id},
        )
    return run_sql(
        "DELETE FROM eb_cache WHERE tenant_id = :t AND cache_key = :k",
        {"t": tenant_id, "k": cache_key},
    )


# ─── Sync metadata ────────────────────────────────────────────────────────

def sync_meta_update(
    tenant_id: str,
    cache_key: str,
    *,
    duration_s: float,
    item_count: int,
    error_count: int = 0,
    last_error: str | None = None,
) -> None:
    """Record sync result for a cache category."""
    run_sql(
        """
        INSERT INTO eb_sync_meta
            (tenant_id, cache_key, last_sync_at, sync_duration_s,
             item_count, error_count, last_error, updated_at)
        VALUES (:t, :k, now(), :dur, :cnt, :err, :le, now())
        ON CONFLICT (tenant_id, cache_key) DO UPDATE
        SET last_sync_at    = now(),
            sync_duration_s = :dur,
            item_count      = :cnt,
            error_count     = :err,
            last_error      = :le,
            updated_at      = now()
        """,
        {
            "t": tenant_id, "k": cache_key,
            "dur": duration_s, "cnt": item_count,
            "err": error_count, "le": last_error,
        },
    )


def needs_sync(tenant_id: str, cache_key: str, interval_s: int) -> bool:
    """Return True if the cache category has never synced or is older than *interval_s*."""
    rows = run_sql(
        """
        SELECT last_sync_at
        FROM eb_sync_meta
        WHERE tenant_id = :t AND cache_key = :k
          AND last_sync_at > now() - make_interval(secs => :iv)
        LIMIT 1
        """,
        {"t": tenant_id, "k": cache_key, "iv": interval_s},
    )
    return not rows


def sync_meta_get(tenant_id: str) -> list[dict[str, Any]]:
    """Return sync metadata for all categories (for admin/status display)."""
    rows = run_sql(
        """
        SELECT cache_key, last_sync_at, sync_duration_s,
               item_count, error_count, last_error, updated_at
        FROM eb_sync_meta
        WHERE tenant_id = :t
        ORDER BY cache_key
        """,
        {"t": tenant_id},
    )
    return rows or []
