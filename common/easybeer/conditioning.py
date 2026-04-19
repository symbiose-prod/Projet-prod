"""
common/easybeer/conditioning.py
===============================
Conditioning planning, barcode matrix, file upload.
"""
from __future__ import annotations

import time as _time
from typing import Any

from ._client import BASE, _auth, _check_response, _log, _throttle, retry_api
from .endpoint import execute_endpoint


@retry_api
def get_planification_matrice(id_brassin: int, id_entrepot: int) -> dict[str, Any]:
    """Matrice conditionnement — L2 DB cache (1h), L3 API."""
    return execute_endpoint(
        method="GET",
        path="brassin/planification-conditionnement/matrice",
        params={"idBrassin": id_brassin, "idEntrepot": id_entrepot},
        cache_key="planification_matrice",
        cache_item_id=f"{id_brassin}_{id_entrepot}",
        cache_ttl=3600,
    )


@retry_api
def add_planification_conditionnement(payload: dict[str, Any]) -> Any:
    """POST /brassin/planification-conditionnement/ajouter → Ajoute une planification."""
    try:
        return execute_endpoint(
            method="POST",
            path="brassin/planification-conditionnement/ajouter",
            payload=payload,
        )
    except Exception:
        # Legacy tolérant : certaines réponses EasyBeer sont des 200 avec body vide
        # (ancien comportement « je n'arrive pas à parser mais l'opération a
        # probablement réussi »).
        _log.debug("Erreur parsing reponse planification", exc_info=True)
        return {"status": "ok"}


@retry_api
def get_code_barre_matrice() -> dict[str, Any]:
    """Matrice codes-barres — L2 DB cache (24h), L3 API.

    Les codes-barres changent très rarement (nouveau produit ≈ 1-2×/an),
    d'où le TTL élevé pour limiter les appels à EasyBeer.
    """
    return execute_endpoint(
        method="GET",
        path="parametres/code-barre/matrice",
        cache_key="code_barre_matrice",
        cache_ttl=86400,
    )


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
