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
    result = _safe_json(r, ep)
    # Invalider le cache DB des brassins
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_delete
        tid = current_tenant_id()
        cache_delete(tid, "brassins_en_cours")
        cache_delete(tid, "brassins_planifies")
    except Exception:
        pass
    return result


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


# ─── Cache brassins en cours (thread-safe) ──────────────────────────────────

import threading as _threading

_BRASSINS_EN_COURS_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_BRASSINS_EN_COURS_TTL = 300  # 5 min
_BRASSINS_EN_COURS_LOCK = _threading.Lock()


def get_brassins_en_cours_cached() -> list[dict[str, Any]]:
    """Brassins en cours — L1 in-memory, L2 DB cache, L3 API."""
    # L1: in-memory
    now = _time.monotonic()
    with _BRASSINS_EN_COURS_LOCK:
        cached = _BRASSINS_EN_COURS_CACHE["data"]
        if cached is not None and (now - _BRASSINS_EN_COURS_CACHE["ts"]) < _BRASSINS_EN_COURS_TTL:
            return cached
    # L2: DB cache
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        db_cached = cache_get(current_tenant_id(), "brassins_en_cours", max_age_s=600)
        if db_cached is not None:
            with _BRASSINS_EN_COURS_LOCK:
                _BRASSINS_EN_COURS_CACHE["data"] = db_cached
                _BRASSINS_EN_COURS_CACHE["ts"] = _time.monotonic()
            return db_cached
    except Exception:
        pass
    # L3: API
    data = get_brassins_en_cours()
    with _BRASSINS_EN_COURS_LOCK:
        if data:
            _BRASSINS_EN_COURS_CACHE["data"] = data
            _BRASSINS_EN_COURS_CACHE["ts"] = _time.monotonic()
    return data


def invalidate_brassins_en_cours_cache() -> None:
    """Invalide le cache brassins en cours."""
    with _BRASSINS_EN_COURS_LOCK:
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
def _get_brassin_detail_raw(id_brassin: int) -> dict[str, Any]:
    """GET /brassin/{id} → Detail complet d'un brassin (appel HTTP brut)."""
    ep = f"brassin/{id_brassin}"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    return _safe_json(r, ep)


# ─── Cache détail brassin ─────────────────────────────────────────────────
_BRASSIN_DETAIL_CACHE: dict[int, dict[str, Any]] = {}
_BRASSIN_DETAIL_TS: dict[int, float] = {}
_BRASSIN_DETAIL_TTL = 300  # 5 min
_BRASSIN_DETAIL_LOCK = _threading.Lock()


def get_brassin_detail(id_brassin: int) -> dict[str, Any]:
    """Détail brassin — L1 in-memory, L2 DB cache, L3 API."""
    # L1: in-memory
    now = _time.monotonic()
    with _BRASSIN_DETAIL_LOCK:
        cached = _BRASSIN_DETAIL_CACHE.get(id_brassin)
        if cached is not None and (now - _BRASSIN_DETAIL_TS.get(id_brassin, 0)) < _BRASSIN_DETAIL_TTL:
            return cached
    # L2: DB cache
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        db_cached = cache_get(current_tenant_id(), "brassin_detail", item_id=str(id_brassin), max_age_s=600)
        if db_cached is not None:
            with _BRASSIN_DETAIL_LOCK:
                _BRASSIN_DETAIL_CACHE[id_brassin] = db_cached
                _BRASSIN_DETAIL_TS[id_brassin] = now
            return db_cached
    except Exception:
        pass
    # L3: API
    data = _get_brassin_detail_raw(id_brassin)
    with _BRASSIN_DETAIL_LOCK:
        if data:
            _BRASSIN_DETAIL_CACHE[id_brassin] = data
            _BRASSIN_DETAIL_TS[id_brassin] = now
    return data


def invalidate_brassin_detail_cache(id_brassin: int | None = None) -> None:
    """Invalide le cache détail brassin (un ou tous)."""
    with _BRASSIN_DETAIL_LOCK:
        if id_brassin is not None:
            _BRASSIN_DETAIL_CACHE.pop(id_brassin, None)
            _BRASSIN_DETAIL_TS.pop(id_brassin, None)
        else:
            _BRASSIN_DETAIL_CACHE.clear()
            _BRASSIN_DETAIL_TS.clear()


# ─── Brassins planifiés ──────────────────────────────────────────────────

@retry_api
def get_brassins_planifies(days_ahead: int = 90) -> list[dict[str, Any]]:
    """Brassins planifiés — L2 DB cache, L3 API.

    Returns brassins (PLANIFIE + EN_COURS) that still have pending
    production needs. Includes 30 days of lookback to catch EN_COURS
    brassins that started recently but haven't been conditioned yet.
    """
    # L2: DB cache
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        cached = cache_get(current_tenant_id(), "brassins_planifies", max_age_s=1200)
        if cached is not None:
            return cached
    except Exception:
        pass

    # L3: API
    now = datetime.datetime.now(datetime.UTC)
    date_debut = (now - datetime.timedelta(days=30)).strftime(
        "%Y-%m-%dT00:00:00.000Z"
    )
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

    # Exclure les brassins passés qui ne sont plus en cours
    en_cours_ids: set[int] = set()
    try:
        for b in get_brassins_en_cours_cached():
            bid = b.get("idBrassin")
            if bid:
                en_cours_ids.add(bid)
    except Exception:
        _log.debug("Cannot fetch en_cours for planifiés filter", exc_info=True)

    today_str = now.strftime("%Y%m%d")  # YYYYMMDD for comparison

    def _is_future_or_en_cours(b: dict) -> bool:
        """Keep brews that are today/future OR currently active."""
        bid = b.get("idBrassin")
        if bid and bid in en_cours_ids:
            return True
        # Parse date from brew name (last 8 digits = DDMMYYYY)
        nom = b.get("nom") or ""
        m = re.search(r"(\d{8})$", nom)
        if m:
            ddmmyyyy = m.group(1)
            yyyymmdd = ddmmyyyy[4:8] + ddmmyyyy[2:4] + ddmmyyyy[0:2]
            return yyyymmdd >= today_str
        # Fallback: dateDebutFormulaire (epoch ms)
        raw = b.get("dateDebutFormulaire")
        if isinstance(raw, (int, float)) and raw > 0:
            brew_date = datetime.datetime.fromtimestamp(raw / 1000, tz=datetime.UTC)
            return brew_date.date() >= now.date()
        # No date info → keep it
        return True

    planifies = [
        b for b in all_brassins
        if float(b.get("volume") or 0) >= 100
        and not b.get("annule")
        and _is_future_or_en_cours(b)
    ]
    _log.info(
        "Brassins planifiés: %d/%d (horizon %dj, %d en cours)",
        len(planifies), len(all_brassins), days_ahead, len(en_cours_ids),
    )
    # Écriture opportuniste dans le cache DB
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_put
        cache_put(current_tenant_id(), "brassins_planifies", planifies)
    except Exception:
        pass
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
