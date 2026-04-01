"""
common/easybeer/history.py
==========================
Stock container history (paginated) + matières premières entry history.
"""
from __future__ import annotations

import datetime
import os
import time as _time
from typing import Any

from ._client import BASE, TIMEOUT, _auth, _check_response, _log, _safe_json, get_session, is_rate_limited, retry_api

# ─── Cache historique entrées MP (clé = catégorie, TTL 2h) ──────────────────
_MP_HIST_CACHE: dict[str, list[dict[str, Any]]] = {}
_MP_HIST_TS: dict[str, float] = {}
_MP_HIST_TTL = 7200  # 2 heures — les bons de réception changent rarement


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
                datetime.UTC
            ).strftime("%Y-%m-%dT23:59:59.999Z"),
            "type": "PERIODE_LIBRE",
        }

    if ids_matieres_premieres:
        filtre["idsMatieresPremieres"] = ids_matieres_premieres

    if type_mouvement:
        filtre["typeMouvement"] = type_mouvement

    all_items: list[dict[str, Any]] = []
    page = 1  # EasyBeer uses 1-indexed pages

    while True:
        # Bail out early if rate-limited
        if is_rate_limited() > 0:
            _log.warning("Rate-limit actif, arrêt pagination historique (page %d, %d collectés)", page, len(all_items))
            break

        r = get_session().post(
            f"{BASE}/{ep}",
            params={  # type: ignore[arg-type]
                "numeroPage": page,
                "nombreParPage": per_page,
                "colonneTri": "-date",
            },
            json=filtre,
            auth=_auth(),
            timeout=TIMEOUT,
        )
        _check_response(r, ep)
        data = _safe_json(r, ep)

        items = data.get("liste") or []
        all_items.extend(items)

        total_pages = data.get("totalPages", 1)
        _log.debug(
            "contenant/historique page %d/%d \u2014 %d \u00e9l\u00e9ments",
            page, total_pages, len(items),
        )

        page += 1
        if page > total_pages or not items:
            break

    _log.info(
        "contenant/historique : %d mouvements r\u00e9cup\u00e9r\u00e9s (filtre MP=%s, p\u00e9riode=%s\u2192%s)",
        len(all_items),
        ids_matieres_premieres or "toutes",
        date_debut or "\u221e",
        date_fin or "maintenant",
    )
    return all_items


@retry_api
def _get_mp_historique_entree_raw(
    categorie: str,
    *,
    date_debut: str | None = None,
    date_fin: str | None = None,
) -> list[dict[str, Any]]:
    """POST /stock/matieres-premieres/historique/entree/{categorie} (appel HTTP brut)."""
    ep = f"stock/matieres-premieres/historique/entree/{categorie}"

    filtre: dict[str, Any] = {
        "idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013")),
    }
    if date_debut or date_fin:
        filtre["periodeSelectionnee"] = {
            "dateDebut": date_debut or "2020-01-01T00:00:00.000Z",
            "dateFin": date_fin or datetime.datetime.now(
                datetime.UTC
            ).strftime("%Y-%m-%dT23:59:59.999Z"),
            "type": "PERIODE_LIBRE",
        }

    r = get_session().post(
        f"{BASE}/{ep}",
        json=filtre,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = _safe_json(r, ep)
    result = data if isinstance(data, list) else []
    _log.info(
        "mp/historique/entree/%s : %d entrees (periode=%s→%s)",
        categorie,
        len(result),
        date_debut or "∞",
        date_fin or "maintenant",
    )
    return result


def get_mp_historique_entree(
    categorie: str,
    *,
    date_debut: str | None = None,
    date_fin: str | None = None,
) -> list[dict[str, Any]]:
    """Historique entrées MP — L1 in-memory, L2 DB cache, L3 API.

    Le cache n'est utilisé que pour les requêtes 365j complètes (pas de dates
    personnalisées) car c'est le pattern le plus fréquent (pages Ressources + Stocks).
    """
    use_cache = date_debut is None and date_fin is None
    if use_cache:
        # L1: in-memory
        now = _time.monotonic()
        cached = _MP_HIST_CACHE.get(categorie)
        if cached is not None and (now - _MP_HIST_TS.get(categorie, 0)) < _MP_HIST_TTL:
            _log.debug("mp/historique/entree/%s : cache hit (%d entrées)", categorie, len(cached))
            return cached
        # L2: DB cache
        try:
            from common._session import current_tenant_id
            from common.eb_cache import cache_get
            db_cached = cache_get(current_tenant_id(), "mp_historique", item_id=categorie, max_age_s=7200)
            if db_cached is not None:
                _MP_HIST_CACHE[categorie] = db_cached
                _MP_HIST_TS[categorie] = _time.monotonic()
                return db_cached
        except Exception:
            pass

    # L3: API
    result = _get_mp_historique_entree_raw(categorie, date_debut=date_debut, date_fin=date_fin)

    if use_cache and result:
        _MP_HIST_CACHE[categorie] = result
        _MP_HIST_TS[categorie] = _time.monotonic()

    return result


def invalidate_mp_historique_cache(categorie: str | None = None) -> None:
    """Invalide le cache historique entrées MP (une catégorie ou toutes)."""
    if categorie is not None:
        _MP_HIST_CACHE.pop(categorie, None)
        _MP_HIST_TS.pop(categorie, None)
    else:
        _MP_HIST_CACHE.clear()
        _MP_HIST_TS.clear()
