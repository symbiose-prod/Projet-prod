"""Tests for common/outbox/service.py — outbox DB helpers."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from common.outbox.service import (
    DEFAULT_MAX_ATTEMPTS,
    RETRY_DELAYS_SECONDS,
    OutboxEvent,
    _compute_next_retry_delay,
    enqueue_event,
    get_stats,
    list_pending,
    mark_dead,
    mark_retry,
    mark_sent,
    retry_event,
)

# ─── _compute_next_retry_delay ────────────────────────────────────────────


class TestRetryDelay:

    def test_first_attempt_uses_first_delay(self):
        assert _compute_next_retry_delay(0) == RETRY_DELAYS_SECONDS[0]  # 30s

    def test_growing_attempts_use_growing_delays(self):
        delays = [_compute_next_retry_delay(i) for i in range(10)]
        # Strictement croissant ou égal (les derniers délais sont égaux)
        for i in range(len(delays) - 1):
            assert delays[i] <= delays[i + 1]

    def test_overflow_uses_last_delay(self):
        # Au-delà du max, on reste au dernier délai (pas de crash)
        assert _compute_next_retry_delay(100) == RETRY_DELAYS_SECONDS[-1]


# ─── enqueue_event ────────────────────────────────────────────────────────


class TestEnqueueEvent:

    @patch("common.outbox.service.run_sql")
    def test_basic_enqueue(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = [{"id": 42}]
        eid = enqueue_event(
            tenant_id="tenant-1",
            event_type="brassin.create",
            payload={"nom": "Brassin Test"},
        )
        assert eid == 42
        mock_run_sql.assert_called_once()
        sql, params = mock_run_sql.call_args[0]
        assert "INSERT INTO eb_outbox" in sql
        assert params["tid"] == "tenant-1"
        assert params["et"] == "brassin.create"
        # Payload sérialisé JSON
        assert json.loads(params["pl"]) == {"nom": "Brassin Test"}

    @patch("common.outbox.service.run_sql")
    def test_returns_none_on_db_failure(self, mock_run_sql: MagicMock):
        from sqlalchemy.exc import SQLAlchemyError
        mock_run_sql.side_effect = SQLAlchemyError("DB down")
        eid = enqueue_event(
            tenant_id="t1",
            event_type="brassin.create",
            payload={},
        )
        assert eid is None

    @patch("common.outbox.service.run_sql")
    def test_default_max_attempts(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = [{"id": 1}]
        enqueue_event(tenant_id="t1", event_type="x", payload={})
        params = mock_run_sql.call_args[0][1]
        assert params["mx"] == DEFAULT_MAX_ATTEMPTS

    @patch("common.outbox.service.run_sql")
    def test_custom_max_attempts(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = [{"id": 1}]
        enqueue_event(tenant_id="t1", event_type="x", payload={}, max_attempts=3)
        params = mock_run_sql.call_args[0][1]
        assert params["mx"] == 3

    @patch("common.outbox.service.run_sql")
    def test_user_email_persisted(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = [{"id": 1}]
        enqueue_event(
            tenant_id="t1",
            event_type="x",
            payload={},
            created_by="user@example.com",
        )
        params = mock_run_sql.call_args[0][1]
        assert params["cb"] == "user@example.com"


# ─── list_pending ─────────────────────────────────────────────────────────


class TestListPending:

    @patch("common.outbox.service.run_sql")
    def test_empty(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = []
        events = list_pending()
        assert events == []

    @patch("common.outbox.service.run_sql")
    def test_parses_payload_string_to_dict(self, mock_run_sql: MagicMock):
        """Postgres peut renvoyer le JSONB comme str selon le driver."""
        mock_run_sql.return_value = [
            {
                "id": 1,
                "tenant_id": "t1",
                "event_type": "brassin.create",
                "payload": '{"a": 1}',  # str
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 10,
                "last_error": None,
                "created_by": "x@y.com",
            }
        ]
        events = list_pending()
        assert len(events) == 1
        assert isinstance(events[0], OutboxEvent)
        assert events[0].payload == {"a": 1}

    @patch("common.outbox.service.run_sql")
    def test_handles_dict_payload(self, mock_run_sql: MagicMock):
        """Si le driver renvoie déjà un dict, on garde tel quel."""
        mock_run_sql.return_value = [
            {
                "id": 1,
                "tenant_id": "t1",
                "event_type": "x",
                "payload": {"already": "dict"},  # dict
                "status": "pending",
                "attempt_count": 0,
                "max_attempts": 10,
                "last_error": None,
                "created_by": None,
            }
        ]
        events = list_pending()
        assert events[0].payload == {"already": "dict"}


# ─── mark_sent / mark_retry / mark_dead ──────────────────────────────────


class TestMarkSent:

    @patch("common.outbox.service.run_sql")
    def test_marks_status_sent(self, mock_run_sql: MagicMock):
        mark_sent(42)
        sql, params = mock_run_sql.call_args[0]
        assert "UPDATE eb_outbox" in sql
        assert "status = 'sent'" in sql
        assert "sent_at = now()" in sql
        assert params["id"] == 42


class TestMarkRetry:

    @patch("common.outbox.service.run_sql")
    def test_increments_attempts(self, mock_run_sql: MagicMock):
        mark_retry(42, "Connection refused", attempt_count=3)
        sql, params = mock_run_sql.call_args[0]
        assert "attempt_count = :n" in sql
        assert params["n"] == 3
        # attempt_count=3 → délai RETRY_DELAYS_SECONDS[3] (= 15min = 900s)
        assert params["delay"] == str(RETRY_DELAYS_SECONDS[3])

    @patch("common.outbox.service.run_sql")
    def test_truncates_error_to_1000(self, mock_run_sql: MagicMock):
        long_err = "x" * 5000
        mark_retry(1, long_err, attempt_count=1)
        params = mock_run_sql.call_args[0][1]
        assert len(params["err"]) == 1000


class TestMarkDead:

    @patch("common.outbox.service.run_sql")
    def test_sets_status_dead(self, mock_run_sql: MagicMock):
        mark_dead(42, "Final error")
        sql, params = mock_run_sql.call_args[0]
        assert "status = 'dead'" in sql
        assert params["err"] == "Final error"


# ─── retry_event ──────────────────────────────────────────────────────────


class TestRetryEvent:

    @patch("common.outbox.service.run_sql")
    def test_returns_true_on_success(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = [{"id": 1}]
        assert retry_event(1) is True

    @patch("common.outbox.service.run_sql")
    def test_returns_false_if_not_dead(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = []  # WHERE status='dead' n'a rien matché
        assert retry_event(1) is False

    @patch("common.outbox.service.run_sql")
    def test_resets_attempts_and_error(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = [{"id": 1}]
        retry_event(1)
        sql = mock_run_sql.call_args[0][0]
        assert "status = 'pending'" in sql
        assert "attempt_count = 0" in sql
        assert "last_error = NULL" in sql


# ─── get_stats ────────────────────────────────────────────────────────────


class TestGetStats:

    @patch("common.outbox.service.run_sql")
    def test_default_keys_present(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = []  # rien en base
        stats = get_stats()
        assert stats == {"pending": 0, "sent": 0, "dead": 0}

    @patch("common.outbox.service.run_sql")
    def test_aggregates_counts(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = [
            {"status": "pending", "n": 5},
            {"status": "sent", "n": 100},
            {"status": "dead", "n": 2},
        ]
        stats = get_stats()
        assert stats == {"pending": 5, "sent": 100, "dead": 2}

    @patch("common.outbox.service.run_sql")
    def test_filters_by_tenant(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = []
        get_stats(tenant_id="tenant-1")
        sql = mock_run_sql.call_args[0][0]
        assert "WHERE tenant_id = :tid" in sql
