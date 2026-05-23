"""Tests for common/outbox/worker.py — async outbox worker logic."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from common.outbox.handlers import UnknownEventType
from common.outbox.service import OutboxEvent
from common.outbox.worker import _process_event, _run_tick


def _make_event(
    *,
    eid: int = 1,
    event_type: str = "brassin.create",
    attempt_count: int = 0,
    max_attempts: int = 10,
) -> OutboxEvent:
    """Helper pour construire un OutboxEvent de test."""
    return OutboxEvent(
        id=eid,
        tenant_id="tenant-1",
        event_type=event_type,
        payload={"foo": "bar"},
        status="pending",
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        last_error=None,
        created_by="user@example.com",
    )


# ─── _process_event : succès ──────────────────────────────────────────────


class TestProcessEventSuccess:

    @patch("common.outbox.worker.mark_sent")
    @patch("common.outbox.worker.dispatch")
    def test_marks_sent_on_success(self, mock_dispatch: MagicMock, mock_mark_sent: MagicMock):
        mock_dispatch.return_value = {"ok": True}
        event = _make_event(eid=42)

        asyncio.run(_process_event(event))

        mock_dispatch.assert_called_once_with("brassin.create", {"foo": "bar"})
        mock_mark_sent.assert_called_once_with(42)


# ─── _process_event : retry ───────────────────────────────────────────────


class TestProcessEventRetry:

    @patch("common.outbox.worker.mark_retry")
    @patch("common.outbox.worker.mark_dead")
    @patch("common.outbox.worker.dispatch")
    def test_marks_retry_on_transient_error(
        self,
        mock_dispatch: MagicMock,
        mock_mark_dead: MagicMock,
        mock_mark_retry: MagicMock,
    ):
        mock_dispatch.side_effect = ConnectionError("network down")
        event = _make_event(eid=42, attempt_count=2, max_attempts=10)

        asyncio.run(_process_event(event))

        mock_mark_retry.assert_called_once()
        args = mock_mark_retry.call_args[0]
        assert args[0] == 42  # event_id
        assert "ConnectionError" in args[1]  # error message includes exception type
        assert args[2] == 3  # incremented attempt_count
        mock_mark_dead.assert_not_called()


# ─── _process_event : dead-letter ─────────────────────────────────────────


class TestProcessEventDead:

    @patch("common.outbox.worker._capture_drift")
    @patch("common.outbox.worker.mark_dead")
    @patch("common.outbox.worker.mark_retry")
    @patch("common.outbox.worker.dispatch")
    def test_marks_dead_at_max_attempts(
        self,
        mock_dispatch: MagicMock,
        mock_mark_retry: MagicMock,
        mock_mark_dead: MagicMock,
        mock_capture: MagicMock,
    ):
        mock_dispatch.side_effect = RuntimeError("persistent failure")
        # attempt_count=9, max=10 → +1 = 10 = max atteint
        event = _make_event(eid=42, attempt_count=9, max_attempts=10)

        asyncio.run(_process_event(event))

        mock_mark_dead.assert_called_once()
        assert mock_mark_dead.call_args[0][0] == 42
        mock_mark_retry.assert_not_called()
        # Sentry capture appelé
        mock_capture.assert_called_once()

    @patch("common.outbox.worker._capture_drift")
    @patch("common.outbox.worker.mark_dead")
    @patch("common.outbox.worker.dispatch")
    def test_unknown_event_type_marks_dead_immediately(
        self,
        mock_dispatch: MagicMock,
        mock_mark_dead: MagicMock,
        mock_capture: MagicMock,
    ):
        """Un event_type inconnu ne sert à rien de retenter — dead direct."""
        mock_dispatch.side_effect = UnknownEventType("brassin.weird")
        event = _make_event(eid=42, attempt_count=0, max_attempts=10)

        asyncio.run(_process_event(event))

        mock_mark_dead.assert_called_once()
        mock_capture.assert_called_once()


# ─── _run_tick ────────────────────────────────────────────────────────────


class TestRunTick:

    @patch("common.outbox.worker.list_pending")
    def test_empty_queue(self, mock_list_pending: MagicMock):
        mock_list_pending.return_value = []
        n = asyncio.run(_run_tick())
        assert n == 0

    @patch("common.outbox.worker.mark_sent")
    @patch("common.outbox.worker.dispatch")
    @patch("common.outbox.worker.list_pending")
    def test_processes_all_events(
        self,
        mock_list_pending: MagicMock,
        mock_dispatch: MagicMock,
        mock_mark_sent: MagicMock,
    ):
        mock_list_pending.return_value = [
            _make_event(eid=1),
            _make_event(eid=2),
            _make_event(eid=3),
        ]
        mock_dispatch.return_value = {"ok": True}

        n = asyncio.run(_run_tick())

        assert n == 3
        assert mock_mark_sent.call_count == 3

    @patch("common.outbox.worker._process_event")
    @patch("common.outbox.worker.list_pending")
    def test_continues_on_event_failure(
        self,
        mock_list_pending: MagicMock,
        mock_process: MagicMock,
    ):
        """Si un event explose, le worker continue avec les suivants."""
        mock_list_pending.return_value = [
            _make_event(eid=1),
            _make_event(eid=2),
            _make_event(eid=3),
        ]

        # Le 2e event lève une exception catastrophique
        async def side_effect(event: OutboxEvent) -> None:
            if event.id == 2:
                raise RuntimeError("catastrophic")

        mock_process.side_effect = side_effect

        n = asyncio.run(_run_tick())
        # 3 events traités malgré l'erreur du 2e
        assert n == 3
        assert mock_process.call_count == 3
