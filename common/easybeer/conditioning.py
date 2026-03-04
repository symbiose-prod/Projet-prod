"""
common/easybeer/conditioning.py
===============================
Conditioning planning, barcode matrix, file upload.
"""
from __future__ import annotations

import time as _time
from typing import Any

from ._client import BASE, TIMEOUT, _auth, _check_response, _log, _safe_json, get_session, retry_api


@retry_api
def get_planification_matrice(id_brassin: int, id_entrepot: int) -> dict[str, Any]:
    """GET /brassin/planification-conditionnement/matrice → Matrice contenants x packagings."""
    ep = "brassin/planification-conditionnement/matrice"
    r = get_session().get(
        f"{BASE}/{ep}",
        params={"idBrassin": id_brassin, "idEntrepot": id_entrepot},
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    return _safe_json(r, ep)


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
    """GET /parametres/code-barre/matrice → Matrice complete des codes-barres."""
    ep = "parametres/code-barre/matrice"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    return _safe_json(r, ep)


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
        _limited = (
            r.status_code == 429
            or (r.status_code == 400 and "banned" in (r.text or "")[:500].lower())
        )
        if not _limited or attempt >= len(_backoff):
            break
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
