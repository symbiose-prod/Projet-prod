"""
common/easybeer/endpoint.py
===========================
Helper d'exécution d'un endpoint EasyBeer — consolide le boilerplate
(auth + throttle + circuit breaker + retry + HTTP + check + safe_json +
cache L2 DB + parsing typé défensif).

Objectif : passer d'un endpoint typique de 40-60 LOC (code dupliqué à
chaque ajout) à 8-12 LOC déclaratifs.

Usage simple (sans cache, sans modèle) :

    @retry_api
    def get_warehouse_detail(id_entrepot: int) -> dict:
        return execute_endpoint(
            method="GET",
            path=f"parametres/entrepot/{id_entrepot}",
        )

Avec cache L2 DB (réponse partagée entre processus) :

    @retry_api
    def get_autonomie_stocks(window_days: int) -> dict:
        return execute_endpoint(
            method="POST",
            path="indicateur/autonomie-stocks",
            params={"forceRefresh": False},
            payload=_indicator_payload(window_days),
            cache_key="autonomie_stocks",
            cache_item_id=str(window_days),
        )

Avec modèle typé (parsing défensif + IDE autocomplete) :

    @retry_api
    def get_autonomie_stocks_typed(window_days: int) -> AutonomieResponse:
        return execute_endpoint(
            method="POST",
            path="indicateur/autonomie-stocks",
            params={"forceRefresh": False},
            payload=_indicator_payload(window_days),
            cache_key="autonomie_stocks",
            cache_item_id=str(window_days),
            response_model=AutonomieResponse,
        )

Ce qui N'est PAS géré par le helper (garder la main côté caller) :
- Le cache L1 in-memory (spécifique par endpoint, avec logique d'invalidation
  métier). Voir common/easybeer/products.py pour les patterns dédiés.
- La désérialisation binaire (Excel, PDF). Les endpoints qui retournent
  ``bytes`` (ex: ``/export/excel``) doivent continuer à appeler get_session()
  directement.
- Le retry : continuez à décorer la fonction publique avec ``@retry_api``
  (le descriptor ne retient pas ce comportement pour garder le contrôle
  au niveau caller — certains endpoints ne doivent pas retry).
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, TypeVar, runtime_checkable

from ._client import BASE, TIMEOUT, _auth, _check_response, _safe_json, get_session

_log = logging.getLogger("ferment.easybeer.endpoint")

T = TypeVar("T")


@runtime_checkable
class _HasFromDict(Protocol):
    """Modèle typé accepté par ``response_model`` (convention Ferment Station)."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Any: ...


def execute_endpoint(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    response_model: type | None = None,
    cache_key: str | None = None,
    cache_item_id: str = "",
    cache_ttl: int = 1800,
    timeout: int = TIMEOUT,
) -> Any:
    """Exécute un endpoint EasyBeer avec tous les garde-fous.

    Pipeline :
    1. Si ``cache_key`` fourni → tentative L2 DB cache. Hit → retourne
       (éventuellement parsé via ``response_model``).
    2. Sinon → appel HTTP (GET ou POST) avec auth/throttle/circuit-breaker
       automatiques via ``_auth()``.
    3. ``_check_response`` log duration/status + retourne des erreurs lisibles.
    4. ``_safe_json`` parse en JSON avec fallback erreur explicite.
    5. Persiste en cache L2 si ``cache_key`` et réponse non-vide.
    6. Si ``response_model`` présent et expose ``from_dict`` → instancie le
       modèle typé (parsing défensif), sinon retourne le JSON brut.

    Args:
        method: ``"GET"`` ou ``"POST"`` (majuscules).
        path: Chemin relatif à ``BASE`` (sans slash initial, ex:
            ``"indicateur/autonomie-stocks"``).
        params: Query-string params (ex: ``{"forceRefresh": False}``).
        payload: Body JSON pour les POST. Ignoré sur les GET.
        response_model: Dataclass avec méthode ``from_dict(dict) -> T``.
            Si présent, la réponse est parsée en instance de ce modèle.
        cache_key: Clé logique du cache DB (ex: ``"autonomie_stocks"``).
            Si ``None``, pas de cache L2.
        cache_item_id: Sous-identifiant pour différencier les entrées sous
            la même clé (ex: ``str(window_days)``).
        cache_ttl: TTL cache L2 en secondes (défaut 30 min).
        timeout: HTTP timeout (défaut ``TIMEOUT`` du client).

    Returns:
        - Instance de ``response_model`` si fourni ET ``from_dict`` dispo.
        - Sinon : dict / list parsé depuis la réponse JSON.

    Raises:
        EasyBeerError: HTTP error (5xx, 4xx hors rate-limit, circuit ouvert,
            réponse non-JSON).
        ValueError: method non supportée.
    """
    method = method.upper()
    if method not in ("GET", "POST"):
        raise ValueError(f"execute_endpoint: method non supportée '{method}'")

    # ── L2 : DB cache (si activé) ─────────────────────────────────────────
    if cache_key:
        try:
            from common._session import current_tenant_id
            from common.eb_cache import cache_get
            cached = cache_get(
                current_tenant_id(), cache_key,
                item_id=cache_item_id, max_age_s=cache_ttl,
            )
            if cached is not None:
                return _maybe_parse(cached, response_model)
        except Exception:
            _log.debug(
                "Lecture cache L2 échouée (cache_key=%s)", cache_key, exc_info=True,
            )

    # ── L3 : API HTTP ─────────────────────────────────────────────────────
    kwargs: dict[str, Any] = {"auth": _auth(), "timeout": timeout}
    if params is not None:
        kwargs["params"] = params

    if method == "GET":
        r = get_session().get(f"{BASE}/{path}", **kwargs)
    else:  # POST
        if payload is not None:
            kwargs["json"] = payload
        r = get_session().post(f"{BASE}/{path}", **kwargs)

    _check_response(r, path)
    data = _safe_json(r, path)

    # ── Persist cache L2 si activé et réponse utile ───────────────────────
    if cache_key and data:
        try:
            from common._session import current_tenant_id
            from common.eb_cache import cache_put
            cache_put(current_tenant_id(), cache_key, data, item_id=cache_item_id)
        except Exception:
            _log.debug(
                "Écriture cache L2 échouée (cache_key=%s)", cache_key, exc_info=True,
            )

    return _maybe_parse(data, response_model)


def _maybe_parse(data: Any, response_model: type | None) -> Any:
    """Parse ``data`` via ``response_model.from_dict`` si applicable."""
    if response_model is None:
        return data
    if not hasattr(response_model, "from_dict"):
        return data
    return response_model.from_dict(data)
