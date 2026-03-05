"""Tests for common/auth.py — DB-dependent functions (mocked run_sql)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common.auth import (
    _check_lockout,
    _clear_failures,
    _lockout_seconds_for,
    _record_failure,
    authenticate,
    change_password,
    check_tenant_allowed,
    cleanup_expired_failures,
    cleanup_expired_resets,
    cleanup_expired_sessions,
    count_users_in_tenant,
    create_session_token,
    ensure_tenant_id,
    find_user_by_email,
    get_or_create_tenant,
    get_tenant_by_name,
    revoke_session_token,
    verify_session_token,
)

# ─── Tenant functions ─────────────────────────────────────────────────────────


class TestGetTenantByName:
    @patch("common.auth.run_sql")
    def test_found(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"id": "t1", "name": "Test", "created_at": "2026-01-01"}]
        result = get_tenant_by_name("Test")
        assert result is not None
        assert result["id"] == "t1"

    @patch("common.auth.run_sql")
    def test_not_found(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        assert get_tenant_by_name("Missing") is None

    @patch("common.auth.run_sql")
    def test_empty_name(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        assert get_tenant_by_name("  ") is None


class TestGetOrCreateTenant:
    @patch("common.auth.run_sql")
    def test_existing_tenant(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"id": "t1", "name": "Test", "created_at": "2026-01-01"}]
        result = get_or_create_tenant("Test")
        assert result["id"] == "t1"

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="Tenant name requis"):
            get_or_create_tenant("")


class TestEnsureTenantId:
    @patch("common.auth.run_sql")
    def test_uuid_passthrough(self, mock_sql: MagicMock):
        uid = "f32b3c7e-1234-5678-9abc-def012345678"
        assert ensure_tenant_id(uid) == uid
        mock_sql.assert_not_called()

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Tenant requis"):
            ensure_tenant_id("")


# ─── User functions ──────────────────────────────────────────────────────────


class TestFindUserByEmail:
    @patch("common.auth.run_sql")
    def test_found(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"id": "u1", "email": "a@b.com", "is_active": True}]
        result = find_user_by_email("a@b.com")
        assert result is not None
        assert result["id"] == "u1"

    @patch("common.auth.run_sql")
    def test_not_found(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        assert find_user_by_email("z@z.com") is None

    def test_empty_email(self):
        assert find_user_by_email("") is None
        assert find_user_by_email(None) is None


class TestCountUsersInTenant:
    @patch("common.auth.run_sql")
    def test_returns_count(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"n": 3}]
        assert count_users_in_tenant("t1") == 3

    @patch("common.auth.run_sql")
    def test_empty(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        assert count_users_in_tenant("t1") == 0


class TestCheckTenantAllowed:
    @patch.dict("os.environ", {"ALLOWED_TENANTS": "Symbiose Kéfir"})
    def test_allowed(self):
        check_tenant_allowed("Symbiose Kéfir")  # should not raise

    @patch.dict("os.environ", {"ALLOWED_TENANTS": "Symbiose Kéfir"})
    def test_not_allowed(self):
        with pytest.raises(ValueError, match="accepte pas"):
            check_tenant_allowed("Other Corp")

    @patch.dict("os.environ", {"ALLOWED_TENANTS": ""})
    def test_no_restriction(self):
        check_tenant_allowed("Anything")  # should not raise


# ─── Lockout functions ───────────────────────────────────────────────────────


class TestLockoutSecondsFor:
    def test_below_threshold(self):
        assert _lockout_seconds_for(3) == 0

    def test_first_threshold(self):
        assert _lockout_seconds_for(5) == 300

    def test_second_threshold(self):
        assert _lockout_seconds_for(10) == 1800

    def test_third_threshold(self):
        assert _lockout_seconds_for(15) == 7200

    def test_above_all(self):
        assert _lockout_seconds_for(20) == 7200


class TestCheckLockout:
    @patch("common.auth.run_sql")
    def test_no_failures(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        assert _check_lockout("a@b.com") is False

    @patch("common.auth.run_sql")
    def test_db_error_returns_false(self, mock_sql: MagicMock):
        mock_sql.side_effect = OSError("DB down")
        assert _check_lockout("a@b.com") is False


class TestRecordFailure:
    @patch("common.auth.run_sql")
    def test_returns_count(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"fail_count": 3}]
        assert _record_failure("a@b.com") == 3

    @patch("common.auth.run_sql")
    def test_db_error_returns_zero(self, mock_sql: MagicMock):
        mock_sql.side_effect = OSError("DB down")
        assert _record_failure("a@b.com") == 0


class TestClearFailures:
    @patch("common.auth.run_sql")
    def test_no_error(self, mock_sql: MagicMock):
        mock_sql.return_value = 1
        _clear_failures("a@b.com")  # should not raise

    @patch("common.auth.run_sql")
    def test_db_error_swallowed(self, mock_sql: MagicMock):
        mock_sql.side_effect = OSError("DB down")
        _clear_failures("a@b.com")  # should not raise


class TestLockoutCaseSensitivity:
    """Verify lockout SQL queries use lower() for case-insensitive matching."""

    @patch("common.auth.run_sql")
    def test_check_lockout_uses_lower(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        _check_lockout("User@Example.COM")
        sql_str = str(mock_sql.call_args_list[0][0][0])
        assert "lower(email)" in sql_str.lower() or "lower( email)" in sql_str.lower()

    @patch("common.auth.run_sql")
    def test_record_failure_uses_lower(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"fail_count": 1}]
        _record_failure("User@Example.COM")
        sql_str = str(mock_sql.call_args_list[0][0][0])
        assert "lower(:e)" in sql_str.lower() or "lower( :e)" in sql_str.lower()

    @patch("common.auth.run_sql")
    def test_clear_failures_uses_lower(self, mock_sql: MagicMock):
        mock_sql.return_value = 1
        _clear_failures("User@Example.COM")
        sql_str = str(mock_sql.call_args_list[0][0][0])
        assert "lower(email)" in sql_str.lower() or "lower( email)" in sql_str.lower()


# ─── Authenticate ─────────────────────────────────────────────────────────────


class TestAuthenticate:
    def test_empty_email(self):
        assert authenticate("", "pwd") is None

    def test_empty_password(self):
        assert authenticate("a@b.com", "") is None

    @patch("common.auth.run_sql")
    @patch("common.auth._check_lockout", return_value=True)
    def test_locked_out(self, mock_lockout: MagicMock, mock_sql: MagicMock):
        assert authenticate("a@b.com", "password1") is None

    @patch("common.auth.run_sql")
    @patch("common.auth._check_lockout", return_value=False)
    def test_unknown_email_timing_safe(self, mock_lockout: MagicMock, mock_sql: MagicMock):
        mock_sql.return_value = []  # no user found
        result = authenticate("z@z.com", "password1")
        assert result is None


# ─── Session tokens ──────────────────────────────────────────────────────────


class TestSessionTokens:
    @patch("common.auth.run_sql")
    def test_create_session_token(self, mock_sql: MagicMock):
        mock_sql.return_value = 1
        token = create_session_token("u1", "t1", days=7)
        assert isinstance(token, str)
        assert len(token) > 20
        mock_sql.assert_called_once()

    @patch("common.auth.run_sql")
    def test_verify_session_token_valid(self, mock_sql: MagicMock):
        mock_sql.return_value = [{
            "id": "u1", "tenant_id": "t1", "email": "a@b.com",
            "role": "admin", "is_active": True,
        }]
        result = verify_session_token("some-token")
        assert result is not None
        assert result["email"] == "a@b.com"

    @patch("common.auth.run_sql")
    def test_verify_session_token_invalid(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        assert verify_session_token("bad-token") is None

    def test_verify_session_token_empty(self):
        assert verify_session_token("") is None
        assert verify_session_token(None) is None

    @patch("common.auth.run_sql")
    def test_revoke_session_token(self, mock_sql: MagicMock):
        mock_sql.return_value = 1
        revoke_session_token("some-token")
        mock_sql.assert_called_once()

    def test_revoke_empty_token(self):
        revoke_session_token("")  # should not raise
        revoke_session_token(None)  # should not raise


# ─── Cleanup ──────────────────────────────────────────────────────────────────


class TestCleanup:
    @patch("common.auth.run_sql")
    def test_cleanup_expired_sessions(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"id": "s1"}, {"id": "s2"}]
        count = cleanup_expired_sessions()
        assert count == 2

    @patch("common.auth.run_sql")
    def test_cleanup_expired_resets(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"id": "r1"}]
        count = cleanup_expired_resets()
        assert count == 1

    @patch("common.auth.run_sql")
    def test_cleanup_expired_failures(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"email": "a@b.com"}]
        count = cleanup_expired_failures()
        assert count == 1


# ─── Change password ─────────────────────────────────────────────────────────


class TestChangePassword:
    @patch("common.auth.run_sql")
    def test_change_password(self, mock_sql: MagicMock):
        mock_sql.return_value = 1
        change_password("u1", "newpassword1")
        mock_sql.assert_called_once()

    def test_change_password_weak(self):
        with pytest.raises(ValueError):
            change_password("u1", "short")
