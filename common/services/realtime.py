"""
common/services/realtime.py
===========================
Broker in-memory pour notifications temps réel multi-comptes (SSE).

Pattern : un dict ``{tenant_id: set[asyncio.Queue]}`` où chaque ``Queue``
représente une connexion SSE active (un onglet web ou un device iOS). Un
``broadcast(tenant_id, event)`` pousse l'event dans toutes les queues du
tenant.

Pourquoi pas WebSocket ?
- SSE est unidirectionnel (serveur → client) et nous suffit largement
  (les clients postent leurs actions via les endpoints REST existants).
- SSE passe en HTTP/1.1 sans config proxy spéciale (juste désactiver le
  buffering nginx/Caddy via ``X-Accel-Buffering: no``).
- iOS et navigateurs gèrent la reconnexion auto via ``EventSource`` /
  ``URLSession``.

Pourquoi pas Redis ?
- Le VPS tourne en single-process NiceGUI (cf. ``app_nicegui.py:1579``).
  Une queue asyncio en RAM partagée entre coroutines suffit. Si on passe
  multi-worker plus tard, on switchera vers Redis pub/sub.

Thread-safety
-------------
``link_palettes_to_ramasse`` et consorts sont **synchrones**, appelées
depuis les endpoints mobile via ``asyncio.to_thread``. Donc
``broadcast()`` peut être invoqué depuis un thread worker — on utilise
``loop.call_soon_threadsafe`` pour scheduler le ``put_nowait`` sur la
loop principale (capturée à l'init du premier ``subscribe``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

_log = logging.getLogger("ferment.realtime")

# Loop principale capturée au premier subscribe() (FastAPI startup).
_main_loop: asyncio.AbstractEventLoop | None = None

# Subscribers actifs par tenant. Chaque entrée du set = une connexion SSE.
_subscribers: dict[str, set[asyncio.Queue]] = {}

# Taille max par queue : un burst de 100 events sans qu'un client lent
# fasse exploser la mémoire. Au-delà : l'event est droppé + warning.
_QUEUE_MAXSIZE = 100


def _ensure_loop() -> asyncio.AbstractEventLoop | None:
    """Capture lazy la loop principale au premier appel async."""
    global _main_loop
    if _main_loop is None:
        try:
            _main_loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
    return _main_loop


async def subscribe(tenant_id: str) -> AsyncIterator[dict[str, Any]]:
    """Async iterator des events broadcasts pour ce tenant.

    Usage typique (endpoint SSE) ::

        async for event in subscribe(tenant_id):
            yield format_sse_chunk(event)

    L'itérateur boucle jusqu'à cancellation (déconnexion client). Le
    cleanup (suppression de la queue du set) est garanti via ``finally``.
    """
    if not tenant_id:
        raise ValueError("subscribe: tenant_id requis")
    _ensure_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    bucket = _subscribers.setdefault(tenant_id, set())
    bucket.add(q)
    _log.debug(
        "realtime: subscribe tenant=%s (subscribers actifs=%d)",
        tenant_id, len(bucket),
    )
    try:
        while True:
            event = await q.get()
            if event is None:
                # Sentinel de fermeture (close_tenant)
                return
            yield event
    finally:
        bucket.discard(q)
        if not bucket:
            _subscribers.pop(tenant_id, None)
        _log.debug(
            "realtime: unsubscribe tenant=%s (subscribers restants=%d)",
            tenant_id, len(_subscribers.get(tenant_id, ())),
        )


def broadcast(tenant_id: str, event: dict[str, Any]) -> None:
    """Diffuse un event à tous les subscribers du tenant.

    Thread-safe : utilise ``loop.call_soon_threadsafe`` quand appelé
    depuis un thread worker. No-op si aucun subscriber (pas d'allocation).

    Args:
        tenant_id: scope du broadcast (multi-tenant strict).
        event: dict sérialisable JSON. Sera enrichi de ``ts`` (float).
    """
    if not tenant_id:
        return
    bucket = _subscribers.get(tenant_id)
    if not bucket:
        return
    payload = {**event, "ts": time.time()}
    queues = list(bucket)  # snapshot — protège contre mutation pendant itération
    loop = _main_loop

    if loop is None or not loop.is_running():
        # Pas de loop active (cas tests synchrones ou appel pré-startup).
        # On tente un put_nowait direct — fonctionne si on est déjà dans
        # une coroutine sur la même loop.
        for q in queues:
            _put_nowait_safe(q, payload, tenant_id)
        return

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is loop:
        # Même loop : put_nowait direct
        for q in queues:
            _put_nowait_safe(q, payload, tenant_id)
    else:
        # Thread worker : schedule sur la loop principale
        for q in queues:
            loop.call_soon_threadsafe(_put_nowait_safe, q, payload, tenant_id)


def _put_nowait_safe(
    queue: asyncio.Queue, payload: dict[str, Any], tenant_id: str,
) -> None:
    """Push sans bloquer. Drop + warn si la queue est pleine (client lent)."""
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        _log.warning(
            "realtime: queue full pour tenant %s — event droppé (type=%s)",
            tenant_id, payload.get("type"),
        )


async def sse_stream(
    tenant_id: str, *, heartbeat_sec: float = 25.0,
) -> AsyncIterator[str]:
    """Async iterator de chunks SSE prêts à envoyer (avec heartbeat).

    Format conforme spec EventSource :
        event: <type>\\n
        data: <json>\\n
        \\n

    Heartbeat = commentaire ``: ping\\n\\n`` toutes les ``heartbeat_sec``
    secondes (évite que les proxies coupent la connexion inactive).
    """
    import json

    if not tenant_id:
        raise ValueError("sse_stream: tenant_id requis")
    _ensure_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    bucket = _subscribers.setdefault(tenant_id, set())
    bucket.add(q)
    try:
        # Confirmation immédiate de souscription
        yield ":connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=heartbeat_sec)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if event is None:
                return
            evt_type = event.get("type", "message")
            data = json.dumps(event, ensure_ascii=False, default=str)
            yield f"event: {evt_type}\ndata: {data}\n\n"
    finally:
        bucket.discard(q)
        if not bucket:
            _subscribers.pop(tenant_id, None)


def active_subscriber_count(tenant_id: str | None = None) -> int:
    """Nombre de subscribers actifs (debug / monitoring).

    Si ``tenant_id`` est fourni : compte pour ce tenant. Sinon : total
    tous tenants confondus.
    """
    if tenant_id is None:
        return sum(len(s) for s in _subscribers.values())
    return len(_subscribers.get(tenant_id, ()))


def close_tenant(tenant_id: str) -> None:
    """Ferme toutes les connexions SSE d'un tenant (utile en tests / shutdown)."""
    bucket = _subscribers.get(tenant_id)
    if not bucket:
        return
    for q in list(bucket):
        _put_nowait_safe(q, None, tenant_id)  # type: ignore[arg-type]


def _reset_for_tests() -> None:
    """Hard reset du broker (tests uniquement)."""
    global _main_loop
    _subscribers.clear()
    _main_loop = None
