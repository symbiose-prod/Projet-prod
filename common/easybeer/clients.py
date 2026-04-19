"""
common/easybeer/clients.py
==========================
Client management endpoints.
"""
from __future__ import annotations

from typing import Any

from ._client import _log, is_rate_limited, retry_api
from .endpoint import execute_endpoint

_MAX_PAGINATION_PAGES = 50


@retry_api
def get_clients(
    page: int = 0,
    per_page: int = 100,
    sort_by: str = "libelle",
    sort_mode: str = "ASC",
    filtre: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /parametres/client/liste → Page de clients (paginee)."""
    return execute_endpoint(
        method="POST",
        path="parametres/client/liste",
        params={
            "colonneTri": sort_by,
            "mode": sort_mode,
            "nombreParPage": per_page,
            "numeroPage": page,
        },
        payload=filtre or {},
    )


# Cache clé : "all_clients_<sort_by>_<sort_mode>" pour chaque combinaison
# de tri/filtre distincte. Les clients changent peu → TTL 1h raisonnable.
_CLIENTS_CACHE_TTL = 3600


def _clients_cache_key(sort_by: str, sort_mode: str, filtre: dict[str, Any] | None) -> str:
    """Build a stable cache key from pagination params."""
    import hashlib
    import json
    f_str = json.dumps(filtre or {}, sort_keys=True)
    f_hash = hashlib.sha1(f_str.encode()).hexdigest()[:8]
    return f"all_clients_{sort_by}_{sort_mode}_{f_hash}"


def get_all_clients(
    sort_by: str = "libelle",
    sort_mode: str = "ASC",
    filtre: dict[str, Any] | None = None,
    per_page: int = 200,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Recupere TOUS les clients en gerant automatiquement la pagination.

    L2 DB cache (1h TTL) : évite de re-fetcher ~10k clients à chaque appel.
    ``force_refresh=True`` contourne le cache pour forcer un re-fetch.
    """
    cache_key = _clients_cache_key(sort_by, sort_mode, filtre)

    # L2: DB cache
    if not force_refresh:
        try:
            from common._session import current_tenant_id
            from common.eb_cache import cache_get
            cached = cache_get(current_tenant_id(), cache_key, max_age_s=_CLIENTS_CACHE_TTL)
            if cached is not None and isinstance(cached, list):
                _log.debug("get_all_clients: %d clients servis depuis le cache DB", len(cached))
                return cached
        except Exception:
            _log.debug("Lecture cache clients échouée", exc_info=True)

    # L3: API (paginated)
    all_clients: list[dict[str, Any]] = []
    page = 0
    while page < _MAX_PAGINATION_PAGES:
        # Bail out early if rate-limited
        if is_rate_limited() > 0:
            _log.warning("Rate-limit actif, arrêt pagination clients (page %d, %d collectés)", page, len(all_clients))
            break
        resp = get_clients(
            page=page, per_page=per_page, sort_by=sort_by,
            sort_mode=sort_mode, filtre=filtre,
        )
        liste = resp.get("liste") or []
        all_clients.extend(liste)
        total_pages = resp.get("totalPages", 1)
        page += 1
        if page >= total_pages or not liste:
            break
    else:
        _log.warning("get_all_clients : limite de %d pages atteinte", _MAX_PAGINATION_PAGES)

    # Persist to DB cache only if fetch was complete (not interrupted by rate-limit)
    if all_clients and page >= 1:
        try:
            from common._session import current_tenant_id
            from common.eb_cache import cache_put
            cache_put(current_tenant_id(), cache_key, all_clients)
        except Exception:
            _log.debug("Écriture cache clients échouée", exc_info=True)

    return all_clients
