"""
common/outbox/service.py
========================
Helpers DB pour le pattern Outbox : enqueue, list pending, transitions de status.

Le worker (worker.py) utilise ces helpers pour consommer la queue. Le dashboard
admin (pages/admin_eb_outbox.py) les utilise aussi pour afficher l'état.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from db.conn import run_sql

_log = logging.getLogger("ferment.outbox")

# Politique de retry : 10 tentatives max, avec backoff exponentiel.
# Délais en secondes : 30s, 1min, 5min, 15min, 1h, 6h, 1j puis 1j x 3
DEFAULT_MAX_ATTEMPTS = 10
RETRY_DELAYS_SECONDS = [30, 60, 300, 900, 3600, 21600, 86400, 86400, 86400, 86400]


@dataclass
class OutboxEvent:
    """Représentation d'un event de l'outbox."""

    id: int
    tenant_id: str
    event_type: str
    payload: dict[str, Any]
    status: str
    attempt_count: int
    max_attempts: int
    last_error: str | None
    created_by: str | None


def _compute_next_retry_delay(attempt_count: int) -> int:
    """Retourne le délai (s) avant la prochaine tentative."""
    idx = min(attempt_count, len(RETRY_DELAYS_SECONDS) - 1)
    return RETRY_DELAYS_SECONDS[idx]


def enqueue_event(
    *,
    tenant_id: str,
    event_type: str,
    payload: dict[str, Any],
    created_by: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> int | None:
    """Persiste un event dans la table ``eb_outbox`` en status='pending'.

    À appeler dans la même transaction que l'opération métier locale (si
    possible) pour garantir la cohérence. Le worker async consommera l'event
    et le poussera vers Easybeer.

    Retourne l'id de l'event ou None si l'insert échoue (DB indisponible).
    Dans ce cas, l'appelant doit décider : tolérer la perte ou propager.
    """
    try:
        rows = run_sql(
            """
            INSERT INTO eb_outbox (
                tenant_id, event_type, payload, status,
                attempt_count, max_attempts, next_retry_at, created_by
            ) VALUES (
                :tid, :et, CAST(:pl AS jsonb), 'pending',
                0, :mx, now(), :cb
            )
            RETURNING id
            """,
            {
                "tid": tenant_id,
                "et": event_type,
                "pl": json.dumps(payload),
                "mx": max_attempts,
                "cb": created_by,
            },
        )
        eid = int(rows[0]["id"]) if rows else None
        _log.info("Outbox enqueued (id=%s, type=%s, tenant=%s)", eid, event_type, tenant_id)
        return eid
    except (SQLAlchemyError, OSError):
        _log.exception("Échec enqueue outbox (type=%s)", event_type)
        return None


def list_pending(limit: int = 20) -> list[OutboxEvent]:
    """Liste les events à retenter (status='pending' et next_retry_at <= now).

    Triés par ordre chronologique d'arrivée — FIFO pour préserver l'ordre
    métier (un brassin créé avant un autre doit l'être aussi côté EB).
    """
    rows = run_sql(
        """
        SELECT id, tenant_id, event_type, payload, status,
               attempt_count, max_attempts, last_error, created_by
        FROM eb_outbox
        WHERE status = 'pending'
          AND next_retry_at <= now()
        ORDER BY created_at ASC
        LIMIT :lim
        """,
        {"lim": limit},
    ) or []

    out: list[OutboxEvent] = []
    for r in rows:
        payload = r.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        out.append(
            OutboxEvent(
                id=int(r["id"]),
                tenant_id=str(r["tenant_id"]),
                event_type=str(r["event_type"]),
                payload=payload,
                status=str(r["status"]),
                attempt_count=int(r["attempt_count"] or 0),
                max_attempts=int(r["max_attempts"] or DEFAULT_MAX_ATTEMPTS),
                last_error=r.get("last_error"),
                created_by=r.get("created_by"),
            )
        )
    return out


def mark_sent(event_id: int) -> None:
    """Marque un event comme envoyé avec succès."""
    run_sql(
        """
        UPDATE eb_outbox
        SET status = 'sent',
            sent_at = now(),
            last_error = NULL
        WHERE id = :id
        """,
        {"id": event_id},
    )
    _log.info("Outbox id=%s marked SENT", event_id)


def mark_retry(event_id: int, error: str, attempt_count: int) -> None:
    """Incrémente le compteur de tentatives + planifie le prochain retry.

    Le délai suit RETRY_DELAYS_SECONDS (backoff exponentiel).
    """
    delay = _compute_next_retry_delay(attempt_count)
    run_sql(
        """
        UPDATE eb_outbox
        SET attempt_count = :n,
            last_error = :err,
            next_retry_at = now() + (:delay || ' seconds')::interval
        WHERE id = :id
        """,
        {
            "id": event_id,
            "n": attempt_count,
            "err": error[:1000],
            "delay": str(delay),
        },
    )
    _log.warning("Outbox id=%s retry scheduled in %ds (attempt=%d)", event_id, delay, attempt_count)


def mark_dead(event_id: int, error: str) -> None:
    """Marque un event comme mort (max_attempts atteint).

    Le worker appelle aussi sentry_sdk.capture_exception() pour alerter.
    """
    run_sql(
        """
        UPDATE eb_outbox
        SET status = 'dead',
            last_error = :err
        WHERE id = :id
        """,
        {"id": event_id, "err": error[:1000]},
    )
    _log.error("Outbox id=%s marked DEAD : %s", event_id, error[:200])


def retry_event(event_id: int) -> bool:
    """Réinitialise un event dead pour qu'il soit retenté par le worker.

    Utilisé par le dashboard admin (bouton "Retry"). Retourne True si l'event
    existait et a été remis en pending, False sinon.
    """
    rows = run_sql(
        """
        UPDATE eb_outbox
        SET status = 'pending',
            attempt_count = 0,
            last_error = NULL,
            next_retry_at = now()
        WHERE id = :id AND status = 'dead'
        RETURNING id
        """,
        {"id": event_id},
    ) or []
    if rows:
        _log.info("Outbox id=%s reset to PENDING (manual retry)", event_id)
        return True
    return False


def get_stats(tenant_id: str | None = None) -> dict[str, int]:
    """Retourne le nombre d'events par status pour le dashboard admin.

    Si ``tenant_id`` est None, agrège tous tenants confondus.
    """
    where = "WHERE tenant_id = :tid" if tenant_id else ""
    params: dict[str, Any] = {"tid": tenant_id} if tenant_id else {}
    rows = run_sql(
        f"""
        SELECT status, COUNT(*)::int AS n
        FROM eb_outbox
        {where}
        GROUP BY status
        """,
        params,
    ) or []
    out = {"pending": 0, "sent": 0, "dead": 0}
    for r in rows:
        st = str(r.get("status") or "")
        out[st] = int(r.get("n") or 0)
    return out
