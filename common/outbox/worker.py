"""
common/outbox/worker.py
=======================
Worker async qui consomme la queue ``eb_outbox`` et pousse les events vers EB.

Pattern :
- Boucle infinie (asyncio), tick toutes les TICK_INTERVAL secondes
- Récupère les events ``status='pending' AND next_retry_at <= now()`` par lots
- Appelle le handler approprié (cf. handlers.py) via asyncio.to_thread (le
  client EB est synchrone et a son propre throttle 1 req/s)
- Sur succès → mark_sent
- Sur échec → mark_retry (backoff exponentiel) ou mark_dead si max atteint
- Sur dead-letter → capture Sentry pour investigation manuelle

Lancement : asyncio.ensure_future(eb_outbox_worker()) au démarrage de l'app.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from common.outbox.handlers import UnknownEventType, dispatch
from common.outbox.service import (
    OutboxEvent,
    list_pending,
    mark_dead,
    mark_retry,
    mark_sent,
)

_log = logging.getLogger("ferment.outbox.worker")

# Le worker tick toutes les TICK_INTERVAL secondes pour vérifier la queue.
# Plus court = plus réactif mais plus de polling DB. 10s est un bon compromis.
TICK_INTERVAL = 10

# Nombre max d'events traités par tick (protection contre les rafales).
BATCH_SIZE = 20

# Délai initial avant le premier tick (laisse le temps à l'app de démarrer).
STARTUP_DELAY = 15


def _capture_drift(event: OutboxEvent, error: str) -> None:
    """Envoie une alerte Sentry quand un event passe en dead-letter.

    On utilise un tag dédié ``outbox_drift`` pour faciliter le filtrage
    et un extra avec l'event_id + event_type + payload résumé.
    """
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("outbox_drift", "true")
            scope.set_tag("outbox_event_type", event.event_type)
            scope.set_extra("outbox_event_id", event.id)
            scope.set_extra("outbox_tenant_id", event.tenant_id)
            scope.set_extra("outbox_attempt_count", event.attempt_count)
            scope.set_extra("outbox_created_by", event.created_by)
            scope.set_extra("outbox_payload", event.payload)
            sentry_sdk.capture_message(
                f"Outbox dead-letter: {event.event_type} (id={event.id}) — {error[:200]}",
                level="error",
            )
    except Exception:
        # Sentry indisponible ou non configuré — on continue sans bloquer.
        _log.exception("Échec capture Sentry pour outbox id=%s", event.id)


async def _process_event(event: OutboxEvent) -> None:
    """Traite un event : appelle le handler, gère succès/échec/dead-letter."""
    new_attempt = event.attempt_count + 1
    try:
        # Le client EB est synchrone (requests) avec son propre throttle interne.
        # On passe par asyncio.to_thread pour ne pas bloquer la loop.
        await asyncio.to_thread(dispatch, event.event_type, event.payload)
        mark_sent(event.id)
    except UnknownEventType as exc:
        # Event mal formé / handler manquant — dead direct (pas de retry utile)
        err = str(exc)
        _log.error("Outbox id=%s UNKNOWN event_type: %s", event.id, err)
        mark_dead(event.id, err)
        _capture_drift(event, err)
    except Exception as exc:  # noqa: BLE001 - on attrape large pour ne pas tuer le worker
        err = f"{type(exc).__name__}: {exc}"
        if new_attempt >= event.max_attempts:
            mark_dead(event.id, err)
            _capture_drift(event, err)
        else:
            mark_retry(event.id, err, new_attempt)


async def _run_tick() -> int:
    """Exécute un tick : récupère les events pending et les traite.

    Retourne le nombre d'events traités (pour métriques).
    """
    events = await asyncio.to_thread(list_pending, BATCH_SIZE)
    if not events:
        return 0

    _log.info("Outbox tick: %d events to process", len(events))
    for event in events:
        try:
            await _process_event(event)
        except Exception:
            # Erreur catastrophique sur cet event — on log et on continue
            # avec le suivant pour ne pas bloquer la queue.
            _log.exception("Outbox tick: failed to process event id=%s", event.id)
    return len(events)


async def eb_outbox_worker() -> None:
    """Boucle async infinie — register avec ``asyncio.ensure_future()`` au démarrage."""
    # Vérifie qu'EB est configuré, sinon le worker ne sert à rien.
    from common.easybeer import is_configured

    _log.info("EB outbox worker starting (tick=%ds, batch=%d)", TICK_INTERVAL, BATCH_SIZE)
    await asyncio.sleep(STARTUP_DELAY)

    while True:
        try:
            if not is_configured():
                _log.debug("EB not configured — skipping outbox tick")
                await asyncio.sleep(TICK_INTERVAL)
                continue

            await _run_tick()
        except Exception:
            _log.exception("Error in outbox worker tick")

        await asyncio.sleep(TICK_INTERVAL)


# ─── Hook pour tests ──────────────────────────────────────────────────────


def _run_one_tick_sync() -> dict[str, Any]:
    """Variante synchrone d'un tick — utile pour tests unitaires."""
    return asyncio.run(_run_tick_with_stats())


async def _run_tick_with_stats() -> dict[str, Any]:
    n = await _run_tick()
    return {"processed": n}
