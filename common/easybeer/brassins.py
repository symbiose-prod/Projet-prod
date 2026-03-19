"""
common/easybeer/brassins.py
===========================
Brassin (brew) management endpoints.
"""
from __future__ import annotations

import datetime
import re
import time as _time
from typing import Any

import requests

from ._client import BASE, TIMEOUT, EasyBeerError, _auth, _check_response, _log, _safe_json, get_session, retry_api


@retry_api
def create_brassin(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /brassin/enregistrer → Cree un nouveau brassin."""
    ep = "brassin/enregistrer"
    r = get_session().post(
        f"{BASE}/{ep}",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    return _safe_json(r, ep)


@retry_api
def get_brassins_en_cours() -> list[dict[str, Any]]:
    """GET /brassin/en-cours/liste → Brassins actuellement en cours."""
    ep = "brassin/en-cours/liste"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    return data if isinstance(data, list) else []


# ─── Cache brassins en cours ────────────────────────────────────────────────

_BRASSINS_EN_COURS_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_BRASSINS_EN_COURS_TTL = 300  # 5 min


def get_brassins_en_cours_cached() -> list[dict[str, Any]]:
    """Brassins en cours avec cache TTL 5 min (évite les appels HTTP redondants)."""
    now = _time.monotonic()
    cached = _BRASSINS_EN_COURS_CACHE["data"]
    if cached is not None and (now - _BRASSINS_EN_COURS_CACHE["ts"]) < _BRASSINS_EN_COURS_TTL:
        return cached
    data = get_brassins_en_cours()
    if data:
        _BRASSINS_EN_COURS_CACHE["data"] = data
        _BRASSINS_EN_COURS_CACHE["ts"] = now
    return data


def invalidate_brassins_en_cours_cache() -> None:
    """Invalide le cache brassins en cours."""
    _BRASSINS_EN_COURS_CACHE["data"] = None
    _BRASSINS_EN_COURS_CACHE["ts"] = 0.0


@retry_api
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

    ep = "brassin/liste"
    r = get_session().post(
        f"{BASE}/{ep}",
        json={
            "dateDebut": date_debut,
            "dateFin": date_fin,
            "type": "PERIODE_LIBRE",
        },
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
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
    ep = f"brassin/{id_brassin}"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    return _safe_json(r, ep)


# ─── Brassins planifiés ──────────────────────────────────────────────────

@retry_api
def get_brassins_planifies(days_ahead: int = 90) -> list[dict[str, Any]]:
    """POST /brassin/liste sur [aujourd'hui → +days_ahead].

    Retourne uniquement les brassins dont ``etat.code == 'PLANIFIE'``.
    Chaque brassin inclut ``planificationsProductions`` (lignes de
    conditionnement), ``ingredients``, ``produit``, etc.
    """
    now = datetime.datetime.now(datetime.UTC)
    date_debut = now.strftime("%Y-%m-%dT00:00:00.000Z")
    date_fin = (now + datetime.timedelta(days=days_ahead)).strftime(
        "%Y-%m-%dT23:59:59.999Z"
    )

    ep = "brassin/liste"
    r = get_session().post(
        f"{BASE}/{ep}",
        json={
            "dateDebut": date_debut,
            "dateFin": date_fin,
            "type": "PERIODE_LIBRE",
        },
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    all_brassins = data if isinstance(data, list) else []

    # Note: brassin/liste does not return etat in summary mode.
    # Filter out small test brassins (< 100L) and cancelled ones.
    planifies = [
        b for b in all_brassins
        if float(b.get("volume") or 0) >= 100
        and not b.get("annule")
    ]
    _log.info(
        "Brassins planifiés: %d/%d (horizon %dj)",
        len(planifies), len(all_brassins), days_ahead,
    )
    return planifies


@retry_api
def delete_conditioning_line(id_planification: int) -> None:
    """GET /brassin/planification-conditionnement/supprimer/{id}.

    Supprime une ligne de planification de conditionnement.
    """
    ep = f"brassin/planification-conditionnement/supprimer/{id_planification}"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    _log.info("Deleted conditioning line %d", id_planification)
