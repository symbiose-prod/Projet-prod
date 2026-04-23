"""
common/easybeer/stocks.py
=========================
Stock-related endpoints: autonomie, MP lots, stock detail, carton weights.
"""
from __future__ import annotations

import datetime
import os
import time
from typing import Any

import requests

from ._client import (
    BASE,
    TIMEOUT,
    EasyBeerError,
    _auth,
    _check_response,
    _excel_payload,
    _indicator_payload,
    _log,
    _safe_dict,
    _safe_json,
    _safe_list,
    get_session,
    is_rate_limited,
    retry_api,
)

# ─── Autonomie stocks ────────────────────────────────────────────────────────

@retry_api
def get_autonomie_stocks_excel(window_days: int) -> bytes:
    """POST /indicateur/autonomie-stocks/export/excel → Bytes du fichier Excel."""
    r = get_session().post(
        f"{BASE}/indicateur/autonomie-stocks/export/excel",
        json=_excel_payload(window_days),
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "autonomie-stocks/export/excel")
    return r.content


@retry_api
def get_autonomie_stocks_excel_period(date_debut_iso: str, date_fin_iso: str) -> bytes:
    """POST /indicateur/autonomie-stocks/export/excel sur période arbitraire.

    Utilisé par la page Prévisions pour récupérer mois par mois l'historique
    de ventes par produit. Format dates : ``YYYY-MM-DDT00:00:00.000Z``.
    """
    payload = {
        "idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013")),
        "periode": {
            "dateDebut": date_debut_iso,
            "dateFin": date_fin_iso,
            "type": "PERIODE_LIBRE",
        },
    }
    r = get_session().post(
        f"{BASE}/indicateur/autonomie-stocks/export/excel",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "autonomie-stocks/export/excel (period)")
    return r.content


@retry_api
def get_stock_produits_export_excel() -> bytes:
    """POST /stock/produits/export → Bytes du fichier Excel (stock par format).

    Contrairement à autonomie-stocks qui ne liste que les formats avec ventes
    sur la période, cet export liste TOUS les couples (Produit, Conditionnement)
    qui ont un stock tracké dans EasyBeer — même ceux à 0 vente.
    Utilisé pour enrichir l'import d'autonomie avec les formats manquants.
    """
    r = get_session().post(
        f"{BASE}/stock/produits/export",
        json={"idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013"))},
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "stock/produits/export")
    return r.content


@retry_api
def get_autonomie_stocks(window_days: int) -> dict[str, Any]:
    """Autonomie stocks — L2 DB cache + appel API via execute_endpoint.

    Premier endpoint migré vers le helper déclaratif
    :func:`common.easybeer.endpoint.execute_endpoint` — passe de ~25 LOC
    de boilerplate à 8 LOC déclaratives.
    """
    from .endpoint import execute_endpoint
    return execute_endpoint(
        method="POST",
        path="indicateur/autonomie-stocks",
        params={"forceRefresh": False},
        payload=_indicator_payload(window_days),
        cache_key="autonomie_stocks",
        cache_item_id=str(window_days),
        cache_ttl=1800,
    )


def get_autonomie_stocks_typed(window_days: int):
    """Version typée de ``get_autonomie_stocks`` qui retourne un
    :class:`AutonomieResponse` (dataclass avec parsing défensif).

    Préférer cette fonction pour les nouveaux callers : elle garantit qu'un
    champ ``null`` côté EasyBeer ne crash pas côté Python (retourne valeurs
    sûres par défaut + warning log), et expose une API typée pour l'IDE.

    Les callers existants continuent d'utiliser ``get_autonomie_stocks`` (dict)
    jusqu'à migration progressive.
    """
    from .endpoint import execute_endpoint
    from .models import AutonomieResponse
    return execute_endpoint(
        method="POST",
        path="indicateur/autonomie-stocks",
        params={"forceRefresh": False},
        payload=_indicator_payload(window_days),
        cache_key="autonomie_stocks",
        cache_item_id=str(window_days),
        cache_ttl=1800,
        response_model=AutonomieResponse,
    )


# ─── Lots matieres premieres ─────────────────────────────────────────────────

@retry_api
def get_mp_lots(id_matiere_premiere: int) -> list[dict[str, Any]]:
    """GET /stock/matieres-premieres/numero-lot/liste/{id} → Liste des lots."""
    from .endpoint import execute_endpoint
    data = execute_endpoint(
        method="GET",
        path=f"stock/matieres-premieres/numero-lot/liste/{id_matiere_premiere}",
    )
    return data if isinstance(data, list) else []


# ─── Detail stock produit ────────────────────────────────────────────────────

_STOCK_DETAIL_CACHE: dict[int, dict[str, Any]] = {}
_STOCK_DETAIL_CACHE_TS: dict[int, float] = {}
_STOCK_DETAIL_CACHE_TTL = 1800  # 30 minutes


@retry_api
def get_stock_produit_detail(id_stock_produit: int) -> dict[str, Any]:
    """GET /stock/produit/edition/{id} → Detail complet d'un stock produit."""
    from .endpoint import execute_endpoint
    # L1 in-memory keyed par id (TTL 30 min) — garde la politique métier locale
    now = time.monotonic()
    cached_ts = _STOCK_DETAIL_CACHE_TS.get(id_stock_produit, 0.0)
    if id_stock_produit in _STOCK_DETAIL_CACHE and (now - cached_ts) < _STOCK_DETAIL_CACHE_TTL:
        return _STOCK_DETAIL_CACHE[id_stock_produit]

    result = execute_endpoint(
        method="GET",
        path=f"stock/produit/edition/{id_stock_produit}",
        timeout=10,   # TTL court : l'endpoint peut être lent sur stocks étoffés
    )
    _STOCK_DETAIL_CACHE[id_stock_produit] = result
    _STOCK_DETAIL_CACHE_TS[id_stock_produit] = now
    return result


# ─── Stock bouteilles (contenants) ──────────────────────────────────────────

_BOTTLE_STOCK_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_BOTTLE_STOCK_TTL = 3600  # 1 heure


@retry_api
def get_bottle_stock() -> dict[int, float]:
    """GET /stock/bouteilles?idUniteVolume=1 → {idContenant: quantiteVirtuelle}.

    Les bouteilles (CONTENANT) ne sont pas dans /stock/matieres-premieres/all.
    Cet endpoint retourne le stock des bouteilles vides par type de contenant.
    """
    # L1: in-memory
    if _BOTTLE_STOCK_CACHE["data"] is not None and (time.monotonic() - _BOTTLE_STOCK_CACHE["ts"]) < _BOTTLE_STOCK_TTL:
        return _BOTTLE_STOCK_CACHE["data"]
    # L2: DB cache
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        cached = cache_get(current_tenant_id(), "bottle_stock", max_age_s=7200)
        if cached is not None:
            _BOTTLE_STOCK_CACHE["data"] = cached
            _BOTTLE_STOCK_CACHE["ts"] = time.monotonic()
            return cached
    except Exception:
        pass
    # L3: API
    ep = "stock/bouteilles"
    r = get_session().get(
        f"{BASE}/{ep}",
        params={"idUniteVolume": 1},
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    result: dict[int, float] = {}
    for child in _safe_list(data, "consolidationsFilles", "stock/bouteilles"):
        cont = _safe_dict(child, "contenant", "stock/bouteilles")
        cont_id = cont.get("idContenant")
        qty = float(child.get("quantiteVirtuelle", 0) or 0)
        if cont_id is not None:
            result[cont_id] = qty
    if result:
        _BOTTLE_STOCK_CACHE["data"] = result
        _BOTTLE_STOCK_CACHE["ts"] = time.monotonic()
        try:
            from common._session import current_tenant_id
            from common.eb_cache import cache_put
            cache_put(current_tenant_id(), "bottle_stock", result)
        except Exception:
            pass
    _log.info("get_bottle_stock: %d contenants chargés", len(result))
    return result


# ─── Poids cartons (avec cache fichier) ──────────────────────────────────────

_WEIGHTS_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "_carton_weights_cache.json",
)
_WEIGHTS_CACHE_TTL = 24 * 3600  # 24 heures


# ─── Matieres premieres (toutes) ─────────────────────────────────────────────

_MP_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_MP_CACHE_TTL = 3600  # 1 heure


@retry_api
def get_all_matieres_premieres() -> list[dict[str, Any]]:
    """Matières premières — L1 in-memory, L2 DB cache, L3 API (via helper)."""
    # L1: in-memory
    if _MP_CACHE["data"] is not None and (time.monotonic() - _MP_CACHE["ts"]) < _MP_CACHE_TTL:
        return _MP_CACHE["data"]
    # L2 + L3 via helper
    from .endpoint import execute_endpoint
    data = execute_endpoint(
        method="GET",
        path="stock/matieres-premieres/all",
        cache_key="mp_all",
        cache_ttl=7200,
    )
    result = data if isinstance(data, list) else []
    if result:
        _MP_CACHE["data"] = result
        _MP_CACHE["ts"] = time.monotonic()
    _log.info("get_all_matieres_premieres : %d MP chargées", len(result))
    return result


def _load_weights_cache() -> dict[tuple[int, str], float] | None:
    """Charge le cache fichier des poids cartons si encore valide."""
    import json
    try:
        with open(_WEIGHTS_CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
        ts = cache.get("ts", 0)
        if datetime.datetime.now(datetime.UTC).timestamp() - ts > _WEIGHTS_CACHE_TTL:
            return None
        weights: dict[tuple[int, str], float] = {}
        for entry in cache.get("data", []):
            weights[(entry["pid"], entry["fmt"])] = entry["w"]
        return weights
    except (OSError, ValueError, KeyError):
        _log.debug("Erreur chargement cache poids cartons", exc_info=True)
        return None


def _save_weights_cache(weights: dict[tuple[int, str], float]) -> None:
    """Sauvegarde le cache fichier des poids cartons (ecriture atomique via rename + flock)."""
    import fcntl
    import json
    import tempfile
    data = [{"pid": pid, "fmt": fmt, "w": w} for (pid, fmt), w in weights.items()]
    cache = {"ts": datetime.datetime.now(datetime.UTC).timestamp(), "data": data}
    try:
        cache_dir = os.path.dirname(_WEIGHTS_CACHE_PATH)
        os.makedirs(cache_dir, exist_ok=True)
        lock_path = _WEIGHTS_CACHE_PATH + ".lock"
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                fd, tmp_path = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(cache, f)
                    os.replace(tmp_path, _WEIGHTS_CACHE_PATH)
                except BaseException:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except (OSError, ValueError):
        _log.warning("Impossible de sauvegarder le cache poids cartons", exc_info=True)


def fetch_carton_weights() -> dict[tuple[int, str], float]:
    """Poids cartons — L2 DB cache (24h), fichier cache (fallback), L3 API."""
    # L2: DB cache (prioritaire sur le cache fichier)
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_get
        db_cached = cache_get(current_tenant_id(), "carton_weights", max_age_s=86400)
        if db_cached is not None:
            weights: dict[tuple[int, str], float] = {}
            for entry in db_cached:
                weights[(entry["pid"], entry["fmt"])] = entry["w"]
            _log.debug("Cache DB poids cartons: %d entrées", len(weights))
            return weights
    except Exception:
        pass
    # Fichier cache (fallback)
    cached = _load_weights_cache()
    if cached is not None:
        _log.debug("Cache fichier poids cartons valide (%d entrees)", len(cached))
        return cached

    _log.info("Fetch poids cartons depuis EasyBeer (cache expire ou absent)")

    payload = {"idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "0"))}
    r = get_session().post(
        f"{BASE}/stock/produits",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "stock/produits")
    data = _safe_json(r, "stock/produits")

    weights: dict[tuple[int, str], float] = {}
    ban_detected = False
    for prod in _safe_list(data, "consolidationsFilles", "stock/produits"):
        if ban_detected:
            break
        for conso in _safe_list(prod, "consolidationsFilles", "stock/produits"):
            # Check rate-limit before each API call
            if is_rate_limited() > 0:
                _log.warning("Rate-limit actif, arrêt fetch poids cartons (%d collectés)", len(weights))
                ban_detected = True
                break

            sid = conso.get("id")
            if not sid:
                continue

            produit = conso.get("produit") or {}
            id_produit = produit.get("idProduit")
            lot = conso.get("lot") or {}
            cont = conso.get("contenant") or {}
            contenance = float(cont.get("contenance", 0) or 0)
            lot_qty = int(lot.get("quantite", 0) or 0)
            if not (id_produit and contenance and lot_qty):
                continue

            fmt_str = f"{lot_qty}x{int(contenance * 100)}"

            try:
                detail = get_stock_produit_detail(sid)
                poids = float(detail.get("poidsUnitaire", 0) or 0)
                if poids > 0:
                    weights[(id_produit, fmt_str)] = poids
            except (EasyBeerError, requests.RequestException) as _e:
                _log.warning("Erreur fetch detail stock %s", sid, exc_info=True)

    _log.info("Fetch poids cartons termine : %d poids recuperes", len(weights))
    _save_weights_cache(weights)
    # Écriture L2 DB cache
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_put
        db_data = [{"pid": pid, "fmt": fmt, "w": w} for (pid, fmt), w in weights.items()]
        cache_put(current_tenant_id(), "carton_weights", db_data)
    except Exception:
        pass
    return weights
