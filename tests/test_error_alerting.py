# tests/test_error_alerting.py
"""Tests for error alerting module (anti-flood + fire-and-forget)."""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

from common.error_alerting import _COOLDOWN_SECONDS, _should_send, send_error_alert

_TEST_ENDPOINT = "GET:/test"


def _reset_cooldown():
    """Reset cooldown to a clean state (empty dict)."""
    import common.error_alerting as mod
    mod._last_alert_ts = {}


class TestShouldSend:
    """Anti-flood cooldown logic (per-endpoint)."""

    def setup_method(self):
        _reset_cooldown()

    def test_first_call_returns_true(self):
        assert _should_send(_TEST_ENDPOINT) is True

    def test_second_call_within_cooldown_returns_false(self):
        assert _should_send(_TEST_ENDPOINT) is True
        assert _should_send(_TEST_ENDPOINT) is False

    def test_after_cooldown_returns_true(self):
        import common.error_alerting as mod
        assert _should_send(_TEST_ENDPOINT) is True
        # Simulate cooldown elapsed
        mod._last_alert_ts[_TEST_ENDPOINT] = time.monotonic() - _COOLDOWN_SECONDS - 1
        assert _should_send(_TEST_ENDPOINT) is True


class TestSendErrorAlert:
    """Fire-and-forget email sending."""

    def setup_method(self):
        _reset_cooldown()

    @patch.dict("os.environ", {"ENV": "development"})
    def test_skipped_in_development(self):
        """Should not send in dev environment."""
        with patch("common.error_alerting._should_send") as mock_should:
            send_error_alert(
                method="GET", path="/test", status_code=500, request_id="abc123",
            )
            mock_should.assert_not_called()

    @patch.dict("os.environ", {"ENV": "production"})
    @patch("common.error_alerting._should_send", return_value=False)
    def test_skipped_during_cooldown(self, mock_should):
        """Should not send during cooldown."""
        with patch("common.email._post_brevo") as mock_brevo:
            send_error_alert(
                method="GET", path="/test", status_code=500, request_id="abc123",
            )
            # Give thread time to start (if it did)
            time.sleep(0.1)
            mock_brevo.assert_not_called()

    @patch.dict("os.environ", {"ENV": "production", "BREVO_API_KEY": "test", "EMAIL_SENDER": "a@b.fr"})
    @patch("common.email._post_brevo", return_value={"messageId": "ok"})
    def test_sends_email_in_production(self, mock_brevo):
        """Should fire email in a thread."""
        send_error_alert(
            method="POST",
            path="/production",
            status_code=500,
            request_id="test-id",
            user_email="user@test.fr",
            error_detail="Something broke",
        )
        # Wait for the daemon thread
        for t in threading.enumerate():
            if t.name == "error-alert":
                t.join(timeout=2)
        mock_brevo.assert_called_once()
        payload = mock_brevo.call_args[0][1]
        assert "500" in payload["subject"]
        assert "user@test.fr" in payload["htmlContent"]
