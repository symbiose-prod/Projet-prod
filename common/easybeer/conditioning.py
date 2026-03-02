"""
common/easybeer/conditioning.py
===============================
Conditioning planning, barcode matrix, file upload.
"""
from __future__ import annotations

import time as _time
from typing import Any

import requests

from ._client import BASE, TIMEOUT, _auth, _check_response, _log


def get_planification_matrice(id_brassin: int, id_entrepot: int) -> dict[str, Any]:
    """GET /brassin/planification-conditionnement/matrice → Matrice contenants x packagings."""
    r = requests.get(
        f"{BASE}/brassin/planification-conditionnement/matrice",
        params={"idBrassin": id_brassin, "idEntrepot": id_entrepot},
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def add_planification_conditionnement(payload: dict[str, Any]) -> Any:
    """POST /brassin/planification-conditionnement/ajouter → Ajoute une planification."""
    r = requests.post(
        f"{BASE}/brassin/planification-conditionnement/ajouter",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "planification-conditionnement/ajouter")
    try:
        return r.json()
    except Exception:
        _log.debug("Erreur parsing reponse planification", exc_info=True)
        return {"status": "ok"}


def get_code_barre_matrice() -> dict[str, Any]:
    """GET /parametres/code-barre/matrice → Matrice complete des codes-barres."""
    r = requests.get(
        f"{BASE}/parametres/code-barre/matrice",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def upload_fichier_brassin(
    id_brassin: int,
    file_bytes: bytes,
    filename: str,
    commentaire: str = "",
) -> dict[str, Any]:
    """POST /brassin/upload/{id} → Upload un fichier dans le brassin."""
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
            "Upload %s : rate-limited HTTP %d (tentative %d/%d) \u2014 retry dans %ds",
            ep, r.status_code, attempt + 1, len(_backoff), delay,
        )
        _time.sleep(delay)

    _check_response(r, ep)
    try:
        return r.json()
    except Exception:
        _log.debug("Erreur parsing reponse code-barres", exc_info=True)
        return {"status": "ok"}
