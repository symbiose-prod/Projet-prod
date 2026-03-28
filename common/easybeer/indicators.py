"""
common/easybeer/indicators.py
==============================
Indicator endpoints: chiffre d'affaire, volumes, performance.
"""
from __future__ import annotations

import os
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
def get_ca_daily(
    date_debut: str,
    date_fin: str,
    *,
    include_avoir: bool = True,
    include_carnet: bool = True,
) -> dict[str, Any]:
    """POST /indicateur/chiffre-affaire → CA journalier sur une période libre.

    Retourne des données journalières : series[0].values = [{x: "DD/MM/YYYY", y: montant}, ...].
    """
    bid = int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013"))

    payload: dict[str, Any] = {
        "idBrasserie": bid,
        "periode": {
            "type": "PERIODE_LIBRE",
            "dateDebut": date_debut,
            "dateFin": date_fin,
        },
        "typeMontant": "HT",
        "inclureVenteDirecte": True,
        "inclureCommande": True,
        "inclureAvoir": include_avoir,
        "inclureFactureAcompte": True,
        "inclureCommandeCarnet": include_carnet,
        "deduireDroitsAccise": False,
        "deduireFraisLivraison": False,
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
    _log.info("CA daily %s → %s : %d séries", date_debut[:10], date_fin[:10], len(data.get("series") or []))
    return data


@retry_api
def get_ca_mensuel(
    year: int,
    *,
    include_avoir: bool = True,
    include_carnet: bool = True,
    tags: str = "",
    ids_clients_types: list[int] | None = None,
    ids_clients_tournees: list[int] | None = None,
) -> dict[str, Any]:
    """POST /indicateur/chiffre-affaire → CA mensuel avec période de référence N-1.

    Utilise le format complet avec ``periodeCalcul: "MOIS"`` et
    ``periodeReference`` pour obtenir directement 2 séries mensuelles
    (année courante + année de référence).

    Returns ``ModeleIndicateurResultat`` avec 2 séries de 12 valeurs.
    """
    bid = int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013"))

    payload: dict[str, Any] = {
        "idBrasserie": bid,
        "type": "CHIFFRE_AFFAIRE_GLOBAL",
        "periode": {
            "type": "ANNEE_COURANTE",
            "dateDebut": f"{year - 1}-12-31T23:00:00.000Z",
            "dateFin": f"{year}-12-30T23:00:00.000Z",
        },
        "periodeReference": {
            "type": "ANNEE_DERNIERE",
            "dateDebut": f"{year - 1}-01-01T00:00:00.000Z",
            "dateFin": f"{year - 1}-12-31T23:59:59.999Z",
        },
        "periodeCalcul": "MOIS",
        "typeMontant": "HT",
        "inclureVenteDirecte": True,
        "inclureCommande": True,
        "inclureAvoir": include_avoir,
        "inclureFactureAcompte": True,
        "inclureCommandeCarnet": include_carnet,
        "deduireDroitsAccise": False,
        "deduireFraisLivraison": False,
        "ignorerVenteZero": False,
        "ignorerStockAcquitte": False,
        "idsClients": [],
        "idsClientsTypes": ids_clients_types or [],
        "idsCommerciaux": [],
        "idsClientsTournees": ids_clients_tournees or [],
        "idsProduits": [],
        "idsProduitsCategories": [],
        "idsContenants": [],
        "idsPackagings": [],
        "idsContenantsFuts": [],
        "idsEntrepots": [],
        "idsEtapeBrassage": [],
        "typesSortieAutre": [],
        "typesAction": [],
        "tags": tags,
    }

    ep = "indicateur/chiffre-affaire"
    r = get_session().post(
        f"{BASE}/{ep}",
        params={"forceRefresh": True},
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)

    nb_series = len(data.get("series") or [])
    _log.info("CA mensuel %d (avoir=%s, carnet=%s): %d séries", year, include_avoir, include_carnet, nb_series)
    return data
