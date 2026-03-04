"""
common/easybeer/brassins.py
===========================
Brassin (brew) management endpoints.
"""
from __future__ import annotations

import datetime
import re
from typing import Any

import requests

from ._client import BASE, TIMEOUT, EasyBeerError, _auth, _check_response, _log, retry_api


@retry_api
def create_brassin(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /brassin/enregistrer → Cree un nouveau brassin."""
    r = requests.post(
        f"{BASE}/brassin/enregistrer",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "brassin/enregistrer")
    return r.json()


@retry_api
def get_brassins_en_cours() -> list[dict[str, Any]]:
    """GET /brassin/en-cours/liste → Brassins actuellement en cours."""
    r = requests.get(
        f"{BASE}/brassin/en-cours/liste",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def get_brassins_archives(
    nombre: int = 3,
    jours: int = 60,
) -> list[dict[str, Any]]:
    """Retourne les *nombre* brassins les plus recents qui ne sont plus en cours."""
    # 1. IDs des brassins en cours
    en_cours_ids: set[int] = set()
    try:
        for b in get_brassins_en_cours():
            bid = b.get("idBrassin")
            if bid:
                en_cours_ids.add(bid)
    except (EasyBeerError, requests.RequestException):
        _log.warning("Erreur fetch brassins en cours pour archives", exc_info=True)

    # 2. Tous les brassins sur la fenetre
    now = datetime.datetime.now(datetime.UTC)
    date_fin = now.strftime("%Y-%m-%dT23:59:59.999Z")
    date_debut = (now - datetime.timedelta(days=jours)).strftime("%Y-%m-%dT00:00:00.000Z")

    r = requests.post(
        f"{BASE}/brassin/liste",
        json={
            "dateDebut": date_debut,
            "dateFin": date_fin,
            "type": "PERIODE_LIBRE",
        },
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    all_brassins = data if isinstance(data, list) else []

    # 3. Exclure en cours + petits brassins (< 100L)
    archived = [
        b for b in all_brassins
        if b.get("idBrassin") not in en_cours_ids
        and float(b.get("volume") or 0) >= 100
    ]

    def _sort_key(b: dict) -> str:
        nom = b.get("nom") or ""
        m = re.search(r"(\d{8})$", nom)
        if m:
            ddmmyyyy = m.group(1)
            return ddmmyyyy[4:8] + ddmmyyyy[2:4] + ddmmyyyy[0:2]
        raw = b.get("dateDebutFormulaire")
        if isinstance(raw, (int, float)):
            return str(int(raw))
        return "0"

    archived.sort(key=_sort_key, reverse=True)
    return archived[:nombre]


@retry_api
def get_brassin_detail(id_brassin: int) -> dict[str, Any]:
    """GET /brassin/{id} → Detail complet d'un brassin."""
    r = requests.get(
        f"{BASE}/brassin/{id_brassin}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()
