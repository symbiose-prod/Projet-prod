"""
common/easybeer/indicators.py
==============================
Indicator endpoints: chiffre d'affaire, volumes, performance.
"""
from __future__ import annotations

from typing import Any

from ._client import (
    BASE,
    TIMEOUT,
    _auth,
    _check_response,
    _log,
    _safe_json,
    get_session,
    retry_api,
)


@retry_api
def get_chiffre_affaire(
    date_debut: str,
    date_fin: str,
    *,
    id_brasserie: int | None = None,
) -> dict[str, Any]:
    """POST /indicateur/chiffre-affaire → Synthèse CA sur une période.

    Retourne un ``ModeleIndicateurResultat`` avec :
    - ``series`` : liste de ``ModeleSerie`` (clé, valeurs)
    - Chaque valeur : ``{label, x, y}`` (label mois, x = date, y = montant €)

    Parameters
    ----------
    date_debut, date_fin : str
        Dates ISO 8601 (ex: ``"2025-01-01T00:00:00.000Z"``).
    id_brasserie : int, optional
        Override du ID brasserie (défaut: env ``EASYBEER_ID_BRASSERIE``).
    """
    import os

    bid = id_brasserie or int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013"))
    payload = {
        "idBrasserie": bid,
        "periode": {
            "dateDebut": date_debut,
            "dateFin": date_fin,
            "type": "PERIODE_LIBRE",
        },
    }

    ep = "indicateur/chiffre-affaire"
    r = get_session().post(
        f"{BASE}/{ep}",
        params={"forceRefresh": False},
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    _log.info(
        "CA %s → %s : %d séries",
        date_debut[:10], date_fin[:10],
        len(data.get("series") or data.get("serie") and [data["serie"]] or []),
    )
    return data
