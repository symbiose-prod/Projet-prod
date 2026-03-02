"""
common/easybeer/clients.py
==========================
Client management endpoints.
"""
from __future__ import annotations

from typing import Any

import requests

from ._client import BASE, TIMEOUT, _auth, _check_response, _log

_MAX_PAGINATION_PAGES = 50


def get_clients(
    page: int = 0,
    per_page: int = 100,
    sort_by: str = "libelle",
    sort_mode: str = "ASC",
    filtre: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /parametres/client/liste → Page de clients (paginee)."""
    r = requests.post(
        f"{BASE}/parametres/client/liste",
        params={
            "colonneTri": sort_by,
            "mode": sort_mode,
            "nombreParPage": per_page,
            "numeroPage": page,
        },
        json=filtre or {},
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "client/liste")
    return r.json()


def get_all_clients(
    sort_by: str = "libelle",
    sort_mode: str = "ASC",
    filtre: dict[str, Any] | None = None,
    per_page: int = 200,
) -> list[dict[str, Any]]:
    """Recupere TOUS les clients en gerant automatiquement la pagination."""
    all_clients: list[dict[str, Any]] = []
    page = 0
    while page < _MAX_PAGINATION_PAGES:
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
    return all_clients
