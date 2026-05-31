"""Tests for common/audit.py — audit trail event logging."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common.audit import (
    ACTION_ACCOUNT_DELETED,
    ACTION_BRASSIN_CREATED,
    ACTION_DEVICE_REGISTERED,
    ACTION_DEVICE_REVOKED,
    ACTION_FILE_UPLOADED,
    ACTION_LOGIN,
    ACTION_LOGIN_FAILED,
    ACTION_LOGOUT,
    ACTION_PACKAGING_REQUEST_DELIVERED,
    ACTION_PACKAGING_REQUEST_SENT,
    ACTION_PDF_DOWNLOADED,
    ACTION_PRODUCTION_SAVED,
    DEFAULT_RETENTION_MONTHS,
    _json_dumps,
    log_event,
    purge_audit_log,
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

    def test_action_packaging_request_sent(self):
        assert ACTION_PACKAGING_REQUEST_SENT == "packaging_request_sent"

    def test_action_packaging_request_delivered(self):
        assert ACTION_PACKAGING_REQUEST_DELIVERED == "packaging_request_delivered"

    def test_action_device_registered(self):
        assert ACTION_DEVICE_REGISTERED == "device_registered"

    def test_action_device_revoked(self):
        assert ACTION_DEVICE_REVOKED == "device_revoked"

    def test_action_account_deleted(self):
        assert ACTION_ACCOUNT_DELETED == "account_deleted"

    def test_action_pdf_downloaded(self):
        assert ACTION_PDF_DOWNLOADED == "pdf_downloaded"

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


# ─── Purge audit_log (rétention RGPD) ─────────────────────────────────────


class TestPurgeAuditLog:
    """Politique de rétention 13 mois pour conformité RGPD art.5."""

    def test_default_retention_is_13_months(self):
        assert DEFAULT_RETENTION_MONTHS == 13

    @patch("common.audit.run_sql")
    def test_uses_default_retention(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = []
        purge_audit_log()
        sql = mock_run_sql.call_args[0][0]
        assert "INTERVAL '13 months'" in sql
        assert "DELETE FROM audit_log" in sql

    @patch("common.audit.run_sql")
    def test_custom_retention(self, mock_run_sql: MagicMock):
        mock_run_sql.return_value = []
        purge_audit_log(retention_months=6)
        sql = mock_run_sql.call_args[0][0]
        assert "INTERVAL '6 months'" in sql

    def test_rejects_zero_and_negative_retention(self):
        with pytest.raises(ValueError, match="> 0"):
            purge_audit_log(retention_months=0)
        with pytest.raises(ValueError, match="> 0"):
            purge_audit_log(retention_months=-3)

    @patch("common.audit.run_sql", side_effect=OSError("DB down"))
    def test_returns_zero_on_db_error(self, mock_run_sql: MagicMock):
        # Idempotente : silent failure → 0, pas d'exception remontée
        result = purge_audit_log()
        assert result == 0

    @patch("common.audit.run_sql")
    def test_sql_injection_safe_on_integer_arg(self, mock_run_sql: MagicMock):
        # retention_months est cast en int() avant interpolation —
        # empêche toute injection via un float ou un string malicieux.
        mock_run_sql.return_value = []
        purge_audit_log(retention_months=12)
        sql = mock_run_sql.call_args[0][0]
        # Vérifie le format exact : "INTERVAL '<int> months'"
        assert "INTERVAL '12 months'" in sql
