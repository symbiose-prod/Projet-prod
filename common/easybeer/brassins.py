"""
common/easybeer/brassins.py
===========================
Brassin (brew) management endpoints.

Pattern : les endpoints simples (create, list, detail, delete) utilisent
:func:`common.easybeer.endpoint.execute_endpoint` qui consolide auth /
circuit-breaker / retry / HTTP / check / safe_json / cache L2 DB.

Les endpoints qui caches un résultat *processé* (archives, planifiés —
filtrage + tri appliqués avant cache) gèrent cache + processing localement
et n'utilisent execute_endpoint que pour le HTTP brut.
"""
from __future__ import annotations

import datetime
import re
import threading as _threading
import time as _time
from typing import Any

import requests

from ._client import EasyBeerError, _log, retry_api
from .endpoint import execute_endpoint

# ─── Création d'un brassin ──────────────────────────────────────────────────

@retry_api
def create_brassin(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /brassin/enregistrer → Crée un nouveau brassin.

    Après succès, invalide les caches DB ``brassins_en_cours`` et
    ``brassins_planifies`` pour que les prochaines lectures reflètent le
    nouveau brassin sans attendre le TTL.
    """
    result = execute_endpoint(
        method="POST",
        path="brassin/enregistrer",
        payload=payload,
    )
    # Invalider le cache DB des brassins (le nouveau brassin n'y est pas)
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_delete
        tid = current_tenant_id()
        cache_delete(tid, "brassins_en_cours")
        cache_delete(tid, "brassins_planifies")
    except Exception:
        pass
    return result


# ─── Brassins en cours ──────────────────────────────────────────────────────

@retry_api
def get_brassins_en_cours() -> list[dict[str, Any]]:
    """GET /brassin/en-cours/liste → Brassins actuellement en cours (bare HTTP)."""
    data = execute_endpoint(
        method="GET",
        path="brassin/en-cours/liste",
    )
    return data if isinstance(data, list) else []


# Cache L1 thread-safe pour la variante cachée de get_brassins_en_cours
_BRASSINS_EN_COURS_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_BRASSINS_EN_COURS_TTL = 300  # 5 min
_BRASSINS_EN_COURS_LOCK = _threading.Lock()


def get_brassins_en_cours_cached() -> list[dict[str, Any]]:
    """Brassins en cours — L1 in-memory, L2 DB cache, L3 API (via helper)."""
    # L1: in-memory
    now = _time.monotonic()
    with _BRASSINS_EN_COURS_LOCK:
        cached = _BRASSINS_EN_COURS_CACHE["data"]
        ts_before = _BRASSINS_EN_COURS_CACHE["ts"]
        if cached is not None and (now - ts_before) < _BRASSINS_EN_COURS_TTL:
            return cached
    # L2 + L3 via helper (délègue cache_get/cache_put)
    data = execute_endpoint(
        method="GET",
        path="brassin/en-cours/liste",
        cache_key="brassins_en_cours",
        cache_ttl=600,
    )
    result = data if isinstance(data, list) else []
    # L1 update (re-check ts_before pour ne pas écraser une invalidation
    # survenue pendant qu'on fetchait)
    with _BRASSINS_EN_COURS_LOCK:
        if result and _BRASSINS_EN_COURS_CACHE["ts"] == ts_before:
            _BRASSINS_EN_COURS_CACHE["data"] = result
            _BRASSINS_EN_COURS_CACHE["ts"] = _time.monotonic()
    return result


def invalidate_brassins_en_cours_cache() -> None:
    """Invalide le cache L1 brassins en cours (le cache L2 expire seul au TTL)."""
    with _BRASSINS_EN_COURS_LOCK:
        _BRASSINS_EN_COURS_CACHE["data"] = None
        _BRASSINS_EN_COURS_CACHE["ts"] = 0.0


# ─── Brassins archivés (cache L2 d'un résultat processé) ────────────────────

@retry_api
def get_brassins_archives(
    nombre: int = 3,
    jours: int = 60,
) -> list[dict[str, Any]]:
    """Brassins archivés — L2 DB cache (1h) d'un résultat processé, L3 API.

    Le cache L2 stocke le résultat après filtrage (exclusion des en-cours
    et des petits brassins) + tri — pas la réponse API brute. Le helper
    execute_endpoint ne peut donc pas gérer L2 ici ; on le fait à la main.
    """
    _item_key = f"{nombre}_{jours}"
    # L2: DB cache (résultat processé)
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        cached = cache_get(
            current_tenant_id(), "brassins_archives",
            item_id=_item_key, max_age_s=3600,
        )
        if cached is not None:
            return cached
    except Exception:
        pass

    # L3: API — 2 appels (en_cours pour exclure, puis liste sur fenêtre)
    en_cours_ids: set[int] = set()
    try:
        for b in get_brassins_en_cours():
            bid = b.get("idBrassin")
            if bid:
                en_cours_ids.add(bid)
    except (EasyBeerError, requests.RequestException):
        _log.warning("Erreur fetch brassins en cours pour archives", exc_info=True)

    now = datetime.datetime.now(datetime.UTC)
    date_fin = now.strftime("%Y-%m-%dT23:59:59.999Z")
    date_debut = (now - datetime.timedelta(days=jours)).strftime(
        "%Y-%m-%dT00:00:00.000Z",
    )

    data = execute_endpoint(
        method="POST",
        path="brassin/liste",
        payload={
            "dateDebut": date_debut,
            "dateFin": date_fin,
            "type": "PERIODE_LIBRE",
        },
    )
    all_brassins = data if isinstance(data, list) else []

    # Exclure en cours + petits brassins (< 100L)
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
    result = archived[:nombre]

    # Persist en L2 (résultat processé)
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_put
        cache_put(
            current_tenant_id(), "brassins_archives",
            result, item_id=_item_key,
        )
    except Exception:
        pass
    return result


# ─── Détail brassin ─────────────────────────────────────────────────────────

_BRASSIN_DETAIL_CACHE: dict[int, dict[str, Any]] = {}
_BRASSIN_DETAIL_TS: dict[int, float] = {}
_BRASSIN_DETAIL_TTL = 300  # 5 min
_BRASSIN_DETAIL_LOCK = _threading.Lock()


def get_brassin_detail(id_brassin: int) -> dict[str, Any]:
    """Détail brassin — L1 in-memory (keyed), L2 DB cache, L3 API (via helper)."""
    # L1: in-memory
    now = _time.monotonic()
    with _BRASSIN_DETAIL_LOCK:
        cached = _BRASSIN_DETAIL_CACHE.get(id_brassin)
        ts_before = _BRASSIN_DETAIL_TS.get(id_brassin, 0)
        if cached is not None and (now - ts_before) < _BRASSIN_DETAIL_TTL:
            return cached
    # L2 + L3 via helper
    data = execute_endpoint(
        method="GET",
        path=f"brassin/{id_brassin}",
        cache_key="brassin_detail",
        cache_item_id=str(id_brassin),
        cache_ttl=600,
    )
    with _BRASSIN_DETAIL_LOCK:
        if data and _BRASSIN_DETAIL_TS.get(id_brassin, 0) == ts_before:
            _BRASSIN_DETAIL_CACHE[id_brassin] = data
            _BRASSIN_DETAIL_TS[id_brassin] = _time.monotonic()
    return data


def invalidate_brassin_detail_cache(id_brassin: int | None = None) -> None:
    """Invalide le cache L1 détail brassin (un ou tous)."""
    with _BRASSIN_DETAIL_LOCK:
        if id_brassin is not None:
            _BRASSIN_DETAIL_CACHE.pop(id_brassin, None)
            _BRASSIN_DETAIL_TS.pop(id_brassin, None)
        else:
            _BRASSIN_DETAIL_CACHE.clear()
            _BRASSIN_DETAIL_TS.clear()


# ─── Préparation conditionnement (brassin pré-rempli pour mise-en-bouteille) ─


@retry_api
def get_brassin_preparation_conditionnement(id_brassin: int) -> dict[str, Any]:
    """``GET /brassin/preparation-conditionnement/brassin/{id}``.

    Endpoint "préparation à la mise en bouteille" — retourne le squelette
    de payload attendu par ``POST /brassin/deduction-stocks-conditionnement``
    et ``POST /brassin/mise-en-bouteille``, avec notamment :

    - ``modeleBrassin`` : ModeleBrassin complet (42 clés)
    - ``modeleElevage`` : dict ``{}`` si pas d'élevage
    - ``produitsDerives`` : ``[ModeleProduit du brassin]``
    - ``volumeRestant`` : volume non encore conditionné
    - **``modelesStockProduitBouteille``** : arbre ``[{libelle: "FERMENT
      STATION", modelesFils: [{idStockBouteille, libelle, contenance, ...}]}]``
      — ce champ N'EST PAS dans la réponse de ``GET /brassin/{id}`` brute,
      d'où ce endpoint dédié.
    - ``modelesStocksMiseEnBouteille: []`` (vide, sera rempli par
      ``deduction-stocks-conditionnement``)
    - ``dateLimiteUtilisationOptimale`` : DDM calculée

    Cf. ``docs/easybeer-write-payloads/preparation-conditionnement.response.json``
    pour la structure de référence (capturée via HAR EB UI).

    Pas de cache côté client : on veut toujours la dernière vue (stocks
    bouteille dispo peuvent changer entre deux conditionnements).
    """
    from .endpoint import execute_endpoint
    return execute_endpoint(
        method="GET",
        path=f"brassin/preparation-conditionnement/brassin/{id_brassin}",
        timeout=20,
    )


# ─── Brassins planifiés (cache L2 d'un résultat processé) ───────────────────

@retry_api
def get_brassins_planifies(days_ahead: int = 90) -> list[dict[str, Any]]:
    """Brassins planifiés — L2 DB cache (résultat processé), L3 API.

    Renvoie brassins PLANIFIE + EN_COURS qui ont encore des besoins de
    production pendants. Inclut 30j de lookback pour capter les EN_COURS
    récents non encore conditionnés. Comme archives, le cache L2 stocke le
    résultat filtré — pas la réponse API brute.
    """
    # L2: DB cache (résultat processé)
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
        "%Y-%m-%dT00:00:00.000Z",
    )
    date_fin = (now + datetime.timedelta(days=days_ahead)).strftime(
        "%Y-%m-%dT23:59:59.999Z",
    )

    data = execute_endpoint(
        method="POST",
        path="brassin/liste",
        payload={
            "dateDebut": date_debut,
            "dateFin": date_fin,
            "type": "PERIODE_LIBRE",
        },
    )
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
    # Persist en L2 (résultat processé)
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_put
        cache_put(current_tenant_id(), "brassins_planifies", planifies)
    except Exception:
        pass
    return planifies


# ─── Suppression ligne conditionnement ──────────────────────────────────────

@retry_api
def delete_conditioning_line(id_planification: int) -> None:
    """GET /brassin/planification-conditionnement/supprimer/{id}."""
    execute_endpoint(
        method="GET",
        path=f"brassin/planification-conditionnement/supprimer/{id_planification}",
    )
    _log.info("Deleted conditioning line %d", id_planification)
