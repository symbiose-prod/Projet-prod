"""Tests unitaires pour unmark_driver_passed de common/ramasse_history.

Mock de ``run_sql`` — pas de DB réelle.
"""
from __future__ import annotations

from unittest.mock import patch

from common.ramasse_history import unmark_driver_passed


class TestUnmarkDriverPassed:
    @patch("common.ramasse_history.current_user_id", return_value="user-1")
    @patch("common.ramasse_history._audit")
    @patch("common.ramasse_history.run_sql")
    def test_unmark_resets_driver_passed_columns(self, mock_run_sql, mock_audit, _mock_uid):
        mock_run_sql.return_value = [{"id": "abc-123"}]
        ok = unmark_driver_passed("abc-123", tenant_id="t1", user_id="user-1")
        assert ok is True
        sql, params = mock_run_sql.call_args[0]
        assert "UPDATE ramasse_history" in sql
        assert "driver_passed    = FALSE" in sql
        assert "driver_passed_at = NULL" in sql
        assert "driver_passed_by = NULL" in sql
        # Idempotence : ne doit toucher que les ramasses actuellement livrées
        assert "driver_passed = TRUE" in sql
        assert params == {"rid": "abc-123", "tid": "t1"}
        mock_audit.assert_called_once()
        action_arg = mock_audit.call_args[0][0]
        assert action_arg == "ramasse_driver_unmarked"
        details = mock_audit.call_args[0][2]
        assert details == {"ramasse_id": "abc-123", "user_id": "user-1"}

    @patch("common.ramasse_history.current_user_id", return_value="user-1")
    @patch("common.ramasse_history._audit")
    @patch("common.ramasse_history.run_sql")
    def test_unmark_returns_false_if_not_currently_delivered(
        self, mock_run_sql, mock_audit, _mock_uid,
    ):
        mock_run_sql.return_value = []  # WHERE driver_passed = TRUE ne matche pas
        ok = unmark_driver_passed("abc-123", tenant_id="t1", user_id="user-1")
        assert ok is False
        mock_audit.assert_not_called()
