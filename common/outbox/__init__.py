"""
common/outbox/
==============
Pattern Outbox pour synchroniser les écritures vers Easybeer de manière
transactionnelle et fiable.

Usage :
    from common.outbox import enqueue_event
    enqueue_event(
        tenant_id=tid,
        event_type="brassin.create",
        payload={"nom": "...", ...},
        created_by="user@example.com",
    )

Le worker async (lancé au démarrage de l'app) consomme les events pending
et les pousse vers EB avec retry exponentiel + dead-letter.

Voir :
- service.py    : helpers DB (enqueue, list_pending, mark_*)
- handlers.py   : dispatcher event_type → callable EB
- worker.py     : boucle async de consommation
"""
from common.outbox.service import (
    OutboxEvent,
    enqueue_event,
    get_stats,
    list_pending,
    mark_dead,
    mark_retry,
    mark_sent,
    retry_event,
)
from common.outbox.worker import eb_outbox_worker

__all__ = [
    "OutboxEvent",
    "eb_outbox_worker",
    "enqueue_event",
    "get_stats",
    "list_pending",
    "mark_dead",
    "mark_retry",
    "mark_sent",
    "retry_event",
]
