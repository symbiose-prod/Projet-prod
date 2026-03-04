"""Tests for common/audit.py — audit trail event logging."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from common.audit import (
    ACTION_BRASSIN_CREATED,
    ACTION_FILE_UPLOADED,
    ACTION_LOGIN,
    ACTION_LOGIN_FAILED,
    ACTION_LOGOUT,
    ACTION_PRODUCTION_SAVED,
    _json_dumps,
    log_event,
)

# ─── Constants exported ───────────────────────────────────────────────────────


class TestAuditConstants:

    def test_action_login(self):
        assert ACTION_LOGIN == "login"

    def test_action_login_failed(self):
        assert ACTION_LOGIN_FAILED == "login_failed"

    def test_action_logout(self):
        assert ACTION_LOGOUT == "logout"

    def test_action_production_saved(self):
        assert ACTION_PRODUCTION_SAVED == "production_saved"

    def test_action_brassin_created(self):
        assert ACTION_BRASSIN_CREATED == "brassin_created"

    def test_action_file_uploaded(self):
        assert ACTION_FILE_UPLOADED == "file_uploaded"


# ─── _json_dumps ─────────────────────────────────────────────────────────────


class TestJsonDumps:

    def test_simple_dict(self):
        result = _json_dumps({"key": "value"})
        assert '"key"' in result
        assert '"value"' in result

    def test_non_serializable_uses_str(self):
        """default=str handles non-serializable objects."""
        import datetime
        result = _json_dumps({"dt": datetime.date(2026, 3, 4)})
        assert "2026-03-04" in result

    def test_unicode_preserved(self):
        result = _json_dumps({"name": "Kéfir"})
        assert "Kéfir" in result  # ensure_ascii=False

    def test_empty_dict(self):
        result = _json_dumps({})
        assert result == "{}"


# ─── log_event ───────────────────────────────────────────────────────────────


class TestLogEvent:

    @patch("common.audit.run_sql")
    def test_calls_run_sql_with_correct_params(self, mock_run_sql: MagicMock):
        log_event(tenant_id="t1", user_email="a@b.com", action="login")
        mock_run_sql.assert_called_once()
        args, kwargs = mock_run_sql.call_args
        sql = args[0]
        params = args[1]
        assert "INSERT INTO audit_log" in sql
        assert params["t"] == "t1"
        assert params["e"] == "a@b.com"
        assert params["a"] == "login"

    @patch("common.audit.run_sql")
    def test_action_truncated_to_50(self, mock_run_sql: MagicMock):
        long_action = "x" * 100
        log_event(action=long_action)
        params = mock_run_sql.call_args[0][1]
        assert len(params["a"]) == 50

    @patch("common.audit.run_sql")
    def test_details_serialized_as_json(self, mock_run_sql: MagicMock):
        log_event(action="test", details={"foo": 42})
        params = mock_run_sql.call_args[0][1]
        assert '"foo"' in params["d"]
        assert "42" in params["d"]

    @patch("common.audit.run_sql")
    def test_none_details_becomes_empty_dict(self, mock_run_sql: MagicMock):
        log_event(action="test", details=None)
        params = mock_run_sql.call_args[0][1]
        assert params["d"] == "{}"

    @patch("common.audit.run_sql", side_effect=OSError("DB down"))
    def test_never_raises_on_os_error(self, mock_run_sql: MagicMock):
        # Should NOT raise — OSError is caught silently
        log_event(action="test")

    @patch("common.audit.run_sql")
    def test_never_raises_on_sqlalchemy_error(self, mock_run_sql: MagicMock):
        from sqlalchemy.exc import SQLAlchemyError
        mock_run_sql.side_effect = SQLAlchemyError("DB down")
        # Should NOT raise — SQLAlchemyError is caught silently
        log_event(action="test")

    @patch("common.audit.run_sql")
    def test_none_tenant_and_email(self, mock_run_sql: MagicMock):
        log_event(action="logout")
        params = mock_run_sql.call_args[0][1]
        assert params["t"] is None
        assert params["e"] is None
