"""
common/easybeer/conditioning.py
===============================
Conditioning planning, barcode matrix, file upload.
"""
from __future__ import annotations

import time as _time
from typing import Any

from ._client import BASE, TIMEOUT, _auth, _check_response, _log, _safe_json, _throttle, get_session, retry_api


@retry_api
def get_planification_matrice(id_brassin: int, id_entrepot: int) -> dict[str, Any]:
    """Matrice conditionnement — L2 DB cache, L3 API."""
    # L2: DB cache
    _item_key = f"{id_brassin}_{id_entrepot}"
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        cached = cache_get(current_tenant_id(), "planification_matrice", item_id=_item_key, max_age_s=3600)
        if cached is not None:
            return cached
    except Exception:
        pass
    # L3: API
    ep = "brassin/planification-conditionnement/matrice"
    r = get_session().get(
        f"{BASE}/{ep}",
        params={"idBrassin": id_brassin, "idEntrepot": id_entrepot},
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_put
        cache_put(current_tenant_id(), "planification_matrice", data, item_id=_item_key)
    except Exception:
        pass
    return data


@retry_api
def add_planification_conditionnement(payload: dict[str, Any]) -> Any:
    """POST /brassin/planification-conditionnement/ajouter → Ajoute une planification."""
    ep = "planification-conditionnement/ajouter"
    r = get_session().post(
        f"{BASE}/brassin/{ep}",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    try:
        return r.json()
    except (ValueError, TypeError):
        _log.debug("Erreur parsing reponse planification", exc_info=True)
        return {"status": "ok"}


@retry_api
def get_code_barre_matrice() -> dict[str, Any]:
    """Matrice codes-barres — L2 DB cache (24h), L3 API."""
    # L2: DB cache (les codes-barres changent très rarement)
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        cached = cache_get(current_tenant_id(), "code_barre_matrice", max_age_s=86400)
        if cached is not None:
            return cached
    except Exception:
        pass
    # L3: API
    ep = "parametres/code-barre/matrice"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_put
        cache_put(current_tenant_id(), "code_barre_matrice", data)
    except Exception:
        pass
    return data


def upload_fichier_brassin(
    id_brassin: int,
    file_bytes: bytes,
    filename: str,
    commentaire: str = "",
) -> dict[str, Any]:
    """POST /brassin/upload/{id} → Upload un fichier dans le brassin.

    Note: n'utilise PAS get_session() car les uploads multipart fichier
    ne bénéficient pas du connection pooling et le Content-Type doit être
    auto-généré par requests pour le boundary multipart.
    """
    import requests

    params: dict[str, str] = {}
    if commentaire:
        params["commentaire"] = commentaire

    ep = f"brassin/upload/{id_brassin}"
    _backoff = (5, 15, 30)

    for attempt in range(len(_backoff) + 1):
        _throttle()  # respecter le rate-limit global avant chaque tentative
        r = requests.post(
            f"{BASE}/{ep}",
            params=params,
            files={
                "fichier": (
                    filename,
                    file_bytes,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            auth=_auth(),
            timeout=60,
        )
        _body_low = (r.text or "")[:500].lower()
        _limited = (
            r.status_code == 429
            or (
                r.status_code == 400
                and any(kw in _body_low for kw in ("limit", "banned", "rate"))
            )
        )
        if not _limited or attempt >= len(_backoff):
            break
        from ._client import _on_rate_limited
        _on_rate_limited()
        delay = _backoff[attempt]
        _log.warning(
            "Upload %s : rate-limited HTTP %d (tentative %d/%d) — retry dans %ds",
            ep, r.status_code, attempt + 1, len(_backoff), delay,
        )
        _time.sleep(delay)

    _check_response(r, ep)
    try:
        return r.json()
    except (ValueError, TypeError):
        _log.debug("Erreur parsing reponse upload", exc_info=True)
        return {"status": "ok"}
