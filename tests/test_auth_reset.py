"""Tests for common/auth_reset.py — password reset flow (mocked run_sql)."""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from common.auth_reset import (
    _hash_token,
    _recent_requests_for_user,
    consume_token_and_set_password,
    create_password_reset,
    verify_reset_token,
    verify_token,
)

# ─── Helpers ────────────────────────────────────────────────────────────────


class TestHashToken:
    def test_deterministic(self):
        assert _hash_token("abc") == _hash_token("abc")

    def test_different_inputs(self):
        assert _hash_token("abc") != _hash_token("xyz")

    def test_returns_hex(self):
        h = _hash_token("test")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex


class TestRecentRequests:
    @patch("common.auth_reset.run_sql")
    def test_returns_rows(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"id": 1, "created_at": "2026-03-01"}]
        result = _recent_requests_for_user("u1")
        assert len(result) == 1
        mock_sql.assert_called_once()


# ─── create_password_reset ──────────────────────────────────────────────────


class TestCreatePasswordReset:
    def test_empty_email(self):
        assert create_password_reset("") is None
        assert create_password_reset(None) is None

    @patch("common.auth_reset.run_sql")
    def test_unknown_email(self, mock_sql: MagicMock):
        mock_sql.return_value = []  # user not found
        assert create_password_reset("z@z.com") is None

    @patch("common.auth_reset.run_sql")
    def test_success(self, mock_sql: MagicMock):
        mock_sql.side_effect = [
            [{"id": "u1", "email": "a@b.com"}],  # SELECT user
            [],  # _recent_requests_for_user → no recent
            1,   # INSERT token
        ]
        url = create_password_reset("a@b.com")
        assert url is not None
        assert "/reset/" in url
        assert len(url) > 30

    @patch("common.auth_reset._now_utc")
    @patch("common.auth_reset.run_sql")
    def test_rate_limited_active_token(self, mock_sql: MagicMock, mock_now: MagicMock):
        now = datetime.datetime(2026, 3, 4, 12, 0, 0, tzinfo=datetime.UTC)
        mock_now.return_value = now
        mock_sql.side_effect = [
            [{"id": "u1", "email": "a@b.com"}],  # SELECT user
            [{  # _recent_requests_for_user → 1 active token
                "id": 1,
                "created_at": now - datetime.timedelta(minutes=10),
                "used_at": None,
                "expires_at": now + datetime.timedelta(minutes=50),
            }],
        ]
        assert create_password_reset("a@b.com") is None

    @patch("common.auth_reset._now_utc")
    @patch("common.auth_reset.run_sql")
    def test_rate_limited_too_recent(self, mock_sql: MagicMock, mock_now: MagicMock):
        now = datetime.datetime(2026, 3, 4, 12, 0, 0, tzinfo=datetime.UTC)
        mock_now.return_value = now
        mock_sql.side_effect = [
            [{"id": "u1", "email": "a@b.com"}],  # SELECT user
            [{  # _recent_requests_for_user → used token, but very recent
                "id": 1,
                "created_at": now - datetime.timedelta(minutes=2),
                "used_at": now - datetime.timedelta(minutes=1),
                "expires_at": now + datetime.timedelta(minutes=50),
            }],
        ]
        assert create_password_reset("a@b.com") is None

    @patch("common.auth_reset.run_sql")
    def test_passes_ip_and_ua(self, mock_sql: MagicMock):
        mock_sql.side_effect = [
            [{"id": "u1", "email": "a@b.com"}],
            [],   # no recent requests
            1,    # INSERT
        ]
        url = create_password_reset("a@b.com", request_ip="1.2.3.4", request_ua="Test/1.0")
        assert url is not None
        # Verify INSERT was called with the IP/UA
        insert_call = mock_sql.call_args_list[2]
        params = insert_call[0][1]
        assert params["ip"] == "1.2.3.4"
        assert params["ua"] == "Test/1.0"

    @patch("common.auth_reset.run_sql")
    def test_passes_meta_dict(self, mock_sql: MagicMock):
        mock_sql.side_effect = [
            [{"id": "u1", "email": "a@b.com"}],
            [],
            1,
        ]
        url = create_password_reset("a@b.com", meta={"ip": "5.6.7.8", "ua": "Bot/2"})
        assert url is not None


# ─── verify_token ───────────────────────────────────────────────────────────


class TestVerifyToken:
    def test_empty_token(self):
        assert verify_token("") is None
        assert verify_token(None) is None

    @patch("common.auth_reset.run_sql")
    def test_unknown_hash(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        assert verify_token("bad-token") is None

    @patch("common.auth_reset._now_utc")
    @patch("common.auth_reset.run_sql")
    def test_valid_token(self, mock_sql: MagicMock, mock_now: MagicMock):
        now = datetime.datetime(2026, 3, 4, 12, 0, 0, tzinfo=datetime.UTC)
        mock_now.return_value = now
        mock_sql.return_value = [{
            "reset_id": 1,
            "user_id": "u1",
            "email": "a@b.com",
            "used_at": None,
            "expires_at": now + datetime.timedelta(minutes=30),
        }]
        result = verify_token("some-token")
        assert result is not None
        assert result["user_id"] == "u1"

    @patch("common.auth_reset._now_utc")
    @patch("common.auth_reset.run_sql")
    def test_expired_token(self, mock_sql: MagicMock, mock_now: MagicMock):
        now = datetime.datetime(2026, 3, 4, 12, 0, 0, tzinfo=datetime.UTC)
        mock_now.return_value = now
        mock_sql.return_value = [{
            "reset_id": 1,
            "user_id": "u1",
            "email": "a@b.com",
            "used_at": None,
            "expires_at": now - datetime.timedelta(minutes=1),  # expired
        }]
        assert verify_token("some-token") is None

    @patch("common.auth_reset._now_utc")
    @patch("common.auth_reset.run_sql")
    def test_already_used_token(self, mock_sql: MagicMock, mock_now: MagicMock):
        now = datetime.datetime(2026, 3, 4, 12, 0, 0, tzinfo=datetime.UTC)
        mock_now.return_value = now
        mock_sql.return_value = [{
            "reset_id": 1,
            "user_id": "u1",
            "email": "a@b.com",
            "used_at": now - datetime.timedelta(minutes=5),  # already used
            "expires_at": now + datetime.timedelta(minutes=30),
        }]
        assert verify_token("some-token") is None


# ─── verify_reset_token (wrapper) ──────────────────────────────────────────


class TestVerifyResetToken:
    @patch("common.auth_reset.verify_token", return_value=None)
    def test_invalid(self, mock_vt: MagicMock):
        ok, msg = verify_reset_token("bad")
        assert ok is False
        assert "invalide" in msg or "expir" in msg

    @patch("common.auth_reset.verify_token", return_value={"user_id": "u1", "email": "a@b.com"})
    def test_valid(self, mock_vt: MagicMock):
        ok, data = verify_reset_token("good")
        assert ok is True
        assert data["user_id"] == "u1"


# ─── consume_token_and_set_password ────────────────────────────────────────


class TestConsumeToken:
    @patch("db.conn.get_engine")
    def test_success(self, mock_engine: MagicMock):
        # Simulate a successful transaction
        mock_conn = MagicMock()
        mock_engine.return_value.begin.return_value.__enter__ = lambda _: mock_conn
        mock_engine.return_value.begin.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 1
        result = consume_token_and_set_password(1, "u1", "newpassword1")
        assert result is True
        assert mock_conn.execute.call_count == 3  # UPDATE reset, UPDATE password, DELETE sessions

    @patch("db.conn.get_engine")
    def test_already_used_raises(self, mock_engine: MagicMock):
        mock_conn = MagicMock()
        mock_engine.return_value.begin.return_value.__enter__ = lambda _: mock_conn
        mock_engine.return_value.begin.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 0  # token already used
        with pytest.raises(ValueError, match="utilisé|utilis"):
            consume_token_and_set_password(1, "u1", "newpassword1")

    def test_weak_password_raises(self):
        with pytest.raises(ValueError):
            consume_token_and_set_password(1, "u1", "short")
