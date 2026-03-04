"""Tests for common/email.py — Brevo transactional email module."""
from __future__ import annotations

import base64
import http.client
import json
from unittest.mock import MagicMock, patch

import pytest

from common.email import (
    EmailSendError,
    _encode_attachments,
    _get_api_key,
    _get_sender_email,
    _get_sender_name,
    _is_brevo_retryable,
    _post_brevo,
    _require_env,
    _strip_html_to_text,
    html_signature,
    send_html_with_pdf,
    send_reset_email,
)

# ─── _get_api_key / _get_sender_email / _get_sender_name ─────────────────────


class TestGetApiKey:

    def test_returns_env_value(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "xkeysib-abc123")
        assert _get_api_key() == "xkeysib-abc123"

    def test_returns_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("BREVO_API_KEY", raising=False)
        assert _get_api_key() == ""


class TestGetSenderEmail:

    def test_prefers_email_sender(self, monkeypatch):
        monkeypatch.setenv("EMAIL_SENDER", "primary@example.com")
        monkeypatch.setenv("SENDER_EMAIL", "fallback@example.com")
        assert _get_sender_email() == "primary@example.com"

    def test_falls_back_to_sender_email(self, monkeypatch):
        monkeypatch.delenv("EMAIL_SENDER", raising=False)
        monkeypatch.setenv("SENDER_EMAIL", "fallback@example.com")
        assert _get_sender_email() == "fallback@example.com"

    def test_default_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("EMAIL_SENDER", raising=False)
        monkeypatch.delenv("SENDER_EMAIL", raising=False)
        assert _get_sender_email() == "hello@symbiose-kefir.fr"


class TestGetSenderName:

    def test_prefers_email_sender_name(self, monkeypatch):
        monkeypatch.setenv("EMAIL_SENDER_NAME", "Primary Name")
        monkeypatch.setenv("SENDER_NAME", "Fallback Name")
        assert _get_sender_name() == "Primary Name"

    def test_falls_back_to_sender_name(self, monkeypatch):
        monkeypatch.delenv("EMAIL_SENDER_NAME", raising=False)
        monkeypatch.setenv("SENDER_NAME", "Fallback Name")
        assert _get_sender_name() == "Fallback Name"

    def test_default_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("EMAIL_SENDER_NAME", raising=False)
        monkeypatch.delenv("SENDER_NAME", raising=False)
        assert _get_sender_name() == "Symbiose Kefir"


# ─── _require_env ─────────────────────────────────────────────────────────────


class TestRequireEnv:

    def test_all_present(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key123")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        monkeypatch.setenv("EMAIL_SENDER_NAME", "Test Sender")
        api_key, sender_email, sender_name = _require_env()
        assert api_key == "key123"
        assert sender_email == "test@example.com"
        assert sender_name == "Test Sender"

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("BREVO_API_KEY", raising=False)
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        with pytest.raises(EmailSendError, match="BREVO_API_KEY"):
            _require_env()

    def test_sender_always_has_fallback(self, monkeypatch):
        """_get_sender_email() always returns a non-empty string (hardcoded fallback)."""
        monkeypatch.setenv("BREVO_API_KEY", "key123")
        monkeypatch.delenv("EMAIL_SENDER", raising=False)
        monkeypatch.delenv("SENDER_EMAIL", raising=False)
        api_key, sender_email, _ = _require_env()
        assert sender_email == "hello@symbiose-kefir.fr"

    def test_only_api_key_missing_raises(self, monkeypatch):
        """When only BREVO_API_KEY is missing, raises for API key only (sender has fallback)."""
        monkeypatch.delenv("BREVO_API_KEY", raising=False)
        monkeypatch.delenv("EMAIL_SENDER", raising=False)
        monkeypatch.delenv("SENDER_EMAIL", raising=False)
        with pytest.raises(EmailSendError, match="BREVO_API_KEY"):
            _require_env()

    def test_uses_default_sender_email(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key123")
        monkeypatch.delenv("EMAIL_SENDER", raising=False)
        monkeypatch.delenv("SENDER_EMAIL", raising=False)
        api_key, sender_email, sender_name = _require_env()
        assert sender_email == "hello@symbiose-kefir.fr"

    def test_uses_default_sender_name(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key123")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        monkeypatch.delenv("EMAIL_SENDER_NAME", raising=False)
        monkeypatch.delenv("SENDER_NAME", raising=False)
        _, _, sender_name = _require_env()
        assert sender_name == "Symbiose Kefir"


# ─── _is_brevo_retryable ─────────────────────────────────────────────────────


class TestIsBrevoRetryable:

    def test_connection_error_is_retryable(self):
        assert _is_brevo_retryable(ConnectionError("reset")) is True

    def test_os_error_is_retryable(self):
        assert _is_brevo_retryable(OSError("network unreachable")) is True

    def test_http_exception_is_retryable(self):
        assert _is_brevo_retryable(http.client.HTTPException("bad gateway")) is True

    def test_email_send_error_with_429_is_retryable(self):
        exc = EmailSendError("Brevo rate-limit atteint (HTTP 429)")
        assert _is_brevo_retryable(exc) is True

    def test_email_send_error_without_429_is_not_retryable(self):
        exc = EmailSendError("Brevo HTTP 500 — server error")
        assert _is_brevo_retryable(exc) is False

    def test_value_error_is_not_retryable(self):
        assert _is_brevo_retryable(ValueError("bad value")) is False

    def test_runtime_error_is_not_retryable(self):
        assert _is_brevo_retryable(RuntimeError("generic")) is False

    def test_timeout_error_is_retryable(self):
        # TimeoutError is a subclass of OSError
        assert _is_brevo_retryable(TimeoutError("timed out")) is True

    def test_remote_disconnected_is_retryable(self):
        exc = http.client.RemoteDisconnected("Remote end closed connection")
        assert _is_brevo_retryable(exc) is True


# ─── _strip_html_to_text ─────────────────────────────────────────────────────


class TestStripHtmlToText:

    def test_empty_string(self):
        assert _strip_html_to_text("") == ""

    def test_none_returns_empty(self):
        assert _strip_html_to_text(None) == ""

    def test_plain_text_unchanged(self):
        assert _strip_html_to_text("Hello world") == "Hello world"

    def test_br_tags_become_newlines(self):
        result = _strip_html_to_text("Line 1<br>Line 2")
        assert "Line 1\nLine 2" == result

    def test_br_self_closing(self):
        result = _strip_html_to_text("Line 1<br/>Line 2")
        assert "Line 1\nLine 2" == result

    def test_br_with_space(self):
        result = _strip_html_to_text("Line 1<br />Line 2")
        assert "Line 1\nLine 2" == result

    def test_p_tags_become_double_newlines(self):
        result = _strip_html_to_text("<p>Para 1</p><p>Para 2</p>")
        assert "Para 1" in result
        assert "Para 2" in result
        assert "\n\n" in result

    def test_nested_tags_stripped(self):
        result = _strip_html_to_text("<div><strong>Bold</strong> <em>italic</em></div>")
        assert "Bold" in result
        assert "italic" in result
        assert "<" not in result

    def test_multiple_spaces_collapsed(self):
        result = _strip_html_to_text("Hello    world")
        assert result == "Hello world"

    def test_excessive_newlines_collapsed(self):
        result = _strip_html_to_text("A<br><br><br><br>B")
        # At most 2 newlines in a row after collapsing
        assert "\n\n\n" not in result

    def test_full_html_email_body(self):
        html = (
            "<p>Bonjour,</p>"
            "<p>Votre <strong>commande</strong> est prete.</p>"
            "<p>Cordialement</p>"
        )
        result = _strip_html_to_text(html)
        assert "Bonjour" in result
        assert "commande" in result
        assert "prete" in result
        assert "<p>" not in result
        assert "<strong>" not in result

    def test_link_tag_stripped(self):
        html = '<a href="https://example.com">Click here</a>'
        result = _strip_html_to_text(html)
        assert result == "Click here"
        assert "<a" not in result


# ─── _encode_attachments ─────────────────────────────────────────────────────


class TestEncodeAttachments:

    def test_none_returns_empty_list(self):
        assert _encode_attachments(None) == []

    def test_empty_list_returns_empty(self):
        assert _encode_attachments([]) == []

    def test_single_attachment(self):
        content = b"PDF file bytes"
        result = _encode_attachments([("report.pdf", content)])
        assert len(result) == 1
        assert result[0]["name"] == "report.pdf"
        assert result[0]["content"] == base64.b64encode(content).decode("ascii")

    def test_multiple_attachments(self):
        attachments = [
            ("file1.pdf", b"content1"),
            ("file2.xlsx", b"content2"),
        ]
        result = _encode_attachments(attachments)
        assert len(result) == 2
        assert result[0]["name"] == "file1.pdf"
        assert result[1]["name"] == "file2.xlsx"

    def test_empty_content_skipped(self):
        attachments = [
            ("good.pdf", b"data"),
            ("empty.pdf", b""),
            ("also_good.pdf", b"more_data"),
        ]
        result = _encode_attachments(attachments)
        assert len(result) == 2
        names = [a["name"] for a in result]
        assert "good.pdf" in names
        assert "also_good.pdf" in names
        assert "empty.pdf" not in names

    def test_none_content_skipped(self):
        # None is falsy, should be skipped
        attachments = [("none.pdf", None)]
        result = _encode_attachments(attachments)
        assert len(result) == 0

    def test_base64_encoding_is_correct(self):
        content = b"\x00\x01\x02\xff"
        result = _encode_attachments([("binary.bin", content)])
        decoded = base64.b64decode(result[0]["content"])
        assert decoded == content


# ─── html_signature ───────────────────────────────────────────────────────────


class TestHtmlSignature:

    def test_contains_sender_name(self, monkeypatch):
        monkeypatch.setenv("EMAIL_SENDER_NAME", "Ferment Team")
        monkeypatch.setenv("EMAIL_SENDER", "hello@ferment.com")
        sig = html_signature()
        assert "Ferment Team" in sig

    def test_contains_sender_email(self, monkeypatch):
        monkeypatch.setenv("EMAIL_SENDER_NAME", "Ferment Team")
        monkeypatch.setenv("EMAIL_SENDER", "hello@ferment.com")
        sig = html_signature()
        assert "hello@ferment.com" in sig

    def test_is_html(self, monkeypatch):
        monkeypatch.setenv("EMAIL_SENDER_NAME", "Name")
        monkeypatch.setenv("EMAIL_SENDER", "e@e.com")
        sig = html_signature()
        assert "<div" in sig
        assert "<strong>" in sig
        assert "<br>" in sig


# ─── Helper: mock HTTP connection for _post_brevo tests ──────────────────────


def _make_mock_conn(status: int, body: str | dict):
    """Build a mock HTTPSConnection context manager returning given status/body."""
    if isinstance(body, dict):
        body = json.dumps(body)
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read.return_value = body.encode("utf-8")

    mock_conn = MagicMock()
    mock_conn.getresponse.return_value = mock_resp
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn


# ─── _post_brevo ──────────────────────────────────────────────────────────────


class TestPostBrevo:
    """Test the _post_brevo function via its unwrapped version to skip retry."""

    @property
    def _fn(self):
        """Access the unwrapped function, bypassing the tenacity retry decorator."""
        return _post_brevo.__wrapped__

    def test_success_201(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "test-key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = _make_mock_conn(201, {"messageId": "abc-123"})
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            result = self._fn("/v3/smtp/email", {"to": "x@y.com"})
        assert result == {"messageId": "abc-123"}

    def test_success_200(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "test-key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = _make_mock_conn(200, {"messageId": "msg-200"})
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            result = self._fn("/v3/smtp/email", {"to": "x@y.com"})
        assert result["messageId"] == "msg-200"

    def test_success_202(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "test-key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = _make_mock_conn(202, {"messageId": "msg-202"})
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            result = self._fn("/v3/smtp/email", {"to": "x@y.com"})
        assert result["messageId"] == "msg-202"

    def test_http_429_raises(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "test-key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = _make_mock_conn(429, "rate limited")
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            with pytest.raises(EmailSendError, match="rate-limit"):
                self._fn("/v3/smtp/email", {})

    def test_http_500_raises(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "test-key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = _make_mock_conn(500, "internal server error")
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            with pytest.raises(EmailSendError, match="500"):
                self._fn("/v3/smtp/email", {})

    def test_http_400_raises(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "test-key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = _make_mock_conn(400, '{"message": "bad request"}')
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            with pytest.raises(EmailSendError, match="400"):
                self._fn("/v3/smtp/email", {})

    def test_connection_error_raises(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "test-key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(side_effect=OSError("Connection refused"))
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            with pytest.raises(EmailSendError, match="Echec connexion"):
                self._fn("/v3/smtp/email", {})

    def test_empty_response_body(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "test-key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = _make_mock_conn(201, "")
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            result = self._fn("/v3/smtp/email", {})
        assert result == {}

    def test_invalid_json_response(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "test-key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = _make_mock_conn(200, "not-json-at-all")
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            result = self._fn("/v3/smtp/email", {})
        assert result == {"raw": "not-json-at-all"}

    def test_sends_correct_headers(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "my-secret-key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = _make_mock_conn(201, {"messageId": "ok"})
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            self._fn("/v3/smtp/email", {"data": "test"})
        call_args = mock_conn.request.call_args
        headers = call_args[1].get("headers") or call_args[0][3]
        assert headers["api-key"] == "my-secret-key"
        assert headers["content-type"] == "application/json"

    def test_sends_correct_path(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        mock_conn = _make_mock_conn(201, "{}")
        with patch("common.email.http.client.HTTPSConnection", return_value=mock_conn):
            self._fn("/v3/smtp/email", {})
        call_args = mock_conn.request.call_args
        assert call_args[0][1] == "/v3/smtp/email"

    def test_missing_env_raises_before_http(self, monkeypatch):
        monkeypatch.delenv("BREVO_API_KEY", raising=False)
        monkeypatch.setenv("EMAIL_SENDER", "test@example.com")
        with pytest.raises(EmailSendError, match="BREVO_API_KEY"):
            self._fn("/v3/smtp/email", {})


# ─── send_reset_email ────────────────────────────────────────────────────────


class TestSendResetEmail:

    def test_payload_structure(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "noreply@test.com")
        monkeypatch.setenv("EMAIL_SENDER_NAME", "Test App")

        captured = {}

        def mock_post_brevo(path, payload):
            captured["path"] = path
            captured["payload"] = payload
            return {"messageId": "msg-reset-001"}

        with patch("common.email._post_brevo", side_effect=mock_post_brevo):
            result = send_reset_email("user@example.com", "https://app.test/reset/token123")

        assert captured["path"] == "/v3/smtp/email"
        p = captured["payload"]
        assert p["sender"]["email"] == "noreply@test.com"
        assert p["sender"]["name"] == "Test App"
        assert p["to"] == [{"email": "user@example.com"}]
        assert "Reinitialisation" in p["subject"]
        assert "https://app.test/reset/token123" in p["htmlContent"]
        assert "https://app.test/reset/token123" in p["textContent"]

    def test_return_format(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "noreply@test.com")
        monkeypatch.setenv("EMAIL_SENDER_NAME", "Test App")

        with patch("common.email._post_brevo", return_value={"messageId": "abc"}):
            result = send_reset_email("user@example.com", "https://app.test/reset/tok")

        assert result["status"] == "sent"
        assert result["provider_msg_id"] == "abc"
        assert result["response"] == {"messageId": "abc"}

    def test_missing_env_propagates(self, monkeypatch):
        monkeypatch.delenv("BREVO_API_KEY", raising=False)
        monkeypatch.delenv("EMAIL_SENDER", raising=False)
        monkeypatch.setenv("SENDER_EMAIL", "")
        with pytest.raises(EmailSendError):
            send_reset_email("user@example.com", "https://x/reset/tok")

    def test_html_content_contains_link(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "test@test.com")

        captured = {}

        def mock_post(path, payload):
            captured["payload"] = payload
            return {"messageId": "m"}

        with patch("common.email._post_brevo", side_effect=mock_post):
            send_reset_email("user@x.com", "https://example.com/reset/ABC")

        assert '<a href="https://example.com/reset/ABC">' in captured["payload"]["htmlContent"]

    def test_provider_msg_id_none_when_missing(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "test@test.com")

        with patch("common.email._post_brevo", return_value={}):
            result = send_reset_email("user@x.com", "https://x/reset/tok")

        assert result["provider_msg_id"] is None


# ─── send_html_with_pdf ──────────────────────────────────────────────────────


class TestSendHtmlWithPdf:

    def test_basic_send_no_attachments(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "sender@test.com")
        monkeypatch.setenv("EMAIL_SENDER_NAME", "Sender")

        captured = {}

        def mock_post(path, payload):
            captured["payload"] = payload
            return {"messageId": "html-001"}

        with patch("common.email._post_brevo", side_effect=mock_post):
            result = send_html_with_pdf("dest@test.com", "Subject Line", "<p>Body</p>")

        p = captured["payload"]
        assert p["sender"]["email"] == "sender@test.com"
        assert p["to"] == [{"email": "dest@test.com"}]
        assert p["subject"] == "Subject Line"
        assert p["htmlContent"] == "<p>Body</p>"
        assert "textContent" in p
        assert "attachment" not in p
        assert result["status"] == "sent"

    def test_with_attachments(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "s@t.com")

        captured = {}

        def mock_post(path, payload):
            captured["payload"] = payload
            return {"messageId": "att-001"}

        attachments = [("fiche.pdf", b"pdf-bytes"), ("extra.xlsx", b"xlsx-bytes")]
        with patch("common.email._post_brevo", side_effect=mock_post):
            send_html_with_pdf("dest@t.com", "Fiche", "<p>Fiche</p>", attachments=attachments)

        p = captured["payload"]
        assert "attachment" in p
        assert len(p["attachment"]) == 2
        assert p["attachment"][0]["name"] == "fiche.pdf"
        decoded_content = base64.b64decode(p["attachment"][0]["content"])
        assert decoded_content == b"pdf-bytes"

    def test_with_reply_to(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "s@t.com")

        captured = {}

        def mock_post(path, payload):
            captured["payload"] = payload
            return {"messageId": "reply-001"}

        with patch("common.email._post_brevo", side_effect=mock_post):
            send_html_with_pdf(
                "dest@t.com", "Subject", "<p>Body</p>", reply_to="manager@t.com"
            )

        assert captured["payload"]["replyTo"] == {"email": "manager@t.com"}

    def test_without_reply_to(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "s@t.com")

        captured = {}

        def mock_post(path, payload):
            captured["payload"] = payload
            return {"messageId": "no-reply-001"}

        with patch("common.email._post_brevo", side_effect=mock_post):
            send_html_with_pdf("dest@t.com", "Subject", "<p>Body</p>")

        assert "replyTo" not in captured["payload"]

    def test_text_content_generated_from_html(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "s@t.com")

        captured = {}

        def mock_post(path, payload):
            captured["payload"] = payload
            return {"messageId": "txt-001"}

        with patch("common.email._post_brevo", side_effect=mock_post):
            send_html_with_pdf("dest@t.com", "Subj", "<p>Hello <strong>World</strong></p>")

        text = captured["payload"]["textContent"]
        assert "Hello" in text
        assert "World" in text
        assert "<strong>" not in text

    def test_return_format(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "s@t.com")

        with patch("common.email._post_brevo", return_value={"messageId": "ret-001"}):
            result = send_html_with_pdf("d@t.com", "S", "<p>B</p>")

        assert result["status"] == "sent"
        assert result["provider_msg_id"] == "ret-001"
        assert result["response"]["messageId"] == "ret-001"

    def test_empty_attachments_not_included(self, monkeypatch):
        monkeypatch.setenv("BREVO_API_KEY", "key")
        monkeypatch.setenv("EMAIL_SENDER", "s@t.com")

        captured = {}

        def mock_post(path, payload):
            captured["payload"] = payload
            return {"messageId": "empty-att-001"}

        # Pass empty list
        with patch("common.email._post_brevo", side_effect=mock_post):
            send_html_with_pdf("d@t.com", "S", "<p>B</p>", attachments=[])

        assert "attachment" not in captured["payload"]

    def test_missing_env_propagates(self, monkeypatch):
        monkeypatch.delenv("BREVO_API_KEY", raising=False)
        monkeypatch.delenv("EMAIL_SENDER", raising=False)
        monkeypatch.setenv("SENDER_EMAIL", "")
        with pytest.raises(EmailSendError):
            send_html_with_pdf("d@t.com", "S", "<p>B</p>")


# ─── EmailSendError ──────────────────────────────────────────────────────────


class TestEmailSendError:

    def test_is_runtime_error(self):
        assert issubclass(EmailSendError, RuntimeError)

    def test_message_preserved(self):
        exc = EmailSendError("test message")
        assert str(exc) == "test message"
