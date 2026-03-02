"""
common/easybeer/history.py
==========================
Stock container history (paginated).
"""
from __future__ import annotations

import datetime
from typing import Any

import requests

from ._client import BASE, TIMEOUT, _auth, _check_response, _log


def get_contenant_historique(
    *,
    date_debut: str | None = None,
    date_fin: str | None = None,
    ids_matieres_premieres: list[int] | None = None,
    type_mouvement: str | None = None,
    per_page: int = 200,
) -> list[dict[str, Any]]:
    """POST /stock/contenant/historique (pagine) → Historique complet des mouvements."""
    ep = "stock/contenant/historique"

    filtre: dict[str, Any] = {}

    if date_debut or date_fin:
        filtre["periode"] = {
            "dateDebut": date_debut or "2020-01-01T00:00:00.000Z",
            "dateFin": date_fin or datetime.datetime.now(
                datetime.timezone.utc
            ).strftime("%Y-%m-%dT23:59:59.999Z"),
            "type": "PERIODE_LIBRE",
        }

    if ids_matieres_premieres:
        filtre["idsMatieresPremieres"] = ids_matieres_premieres

    if type_mouvement:
        filtre["typeMouvement"] = type_mouvement

    all_items: list[dict[str, Any]] = []
    page = 0

    while True:
        r = requests.post(
            f"{BASE}/{ep}",
            params={
                "numeroPage": page,
                "nombreParPage": per_page,
                "colonneTri": "-date",
            },
            json=filtre,
            auth=_auth(),
            timeout=TIMEOUT,
        )
        _check_response(r, ep)
        data = r.json()

        items = data.get("liste") or []
        all_items.extend(items)

        total_pages = data.get("totalPages", 1)
        _log.debug(
            "contenant/historique page %d/%d \u2014 %d \u00e9l\u00e9ments",
            page + 1, total_pages, len(items),
        )

        page += 1
        if page >= total_pages or not items:
            break

    _log.info(
        "contenant/historique : %d mouvements r\u00e9cup\u00e9r\u00e9s (filtre MP=%s, p\u00e9riode=%s\u2192%s)",
        len(all_items),
        ids_matieres_premieres or "toutes",
        date_debut or "\u221e",
        date_fin or "maintenant",
    )
    return all_items
