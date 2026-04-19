"""Tests for common/email_queue — mock run_sql + send_html_with_pdf."""
from __future__ import annotations

import base64
import json
from unittest.mock import patch

from common.email_queue import (
    MAX_ATTEMPTS,
    _decode_attachments,
    _encode_attachments,
    enqueue,
    retry_pending_emails,
    send_with_queue_fallback,
)


class TestEncodeDecodeAttachments:
    def test_roundtrip(self):
        original = [("report.pdf", b"\x00\x01BINARY"), ("data.csv", b"col1;col2\n1;2")]
        encoded = _encode_attachments(original)
        assert len(encoded) == 2
        assert encoded[0]["name"] == "report.pdf"
        # base64
        assert base64.b64decode(encoded[0]["content_b64"]) == b"\x00\x01BINARY"

        restored = _decode_attachments(encoded)
        assert restored == original

    def test_decode_from_json_string(self):
        raw = [{"name": "a.txt", "content_b64": base64.b64encode(b"hello").decode()}]
        restored = _decode_attachments(json.dumps(raw))
        assert restored == [("a.txt", b"hello")]

    def test_decode_invalid_returns_empty(self):
        assert _decode_attachments("not-json") == []
        assert _decode_attachments(None) == []
        assert _decode_attachments({"not": "a list"}) == []


class TestEnqueue:
    @patch("common.email_queue.run_sql")
    def test_insert_with_encoded_attachments(self, mock_run_sql):
        mock_run_sql.return_value = [{"id": 42}]
        qid = enqueue(
            to_emails=["user@test.fr"],
            subject="Hello",
            html_body="<p>Body</p>",
            attachments=[("file.pdf", b"PDF-CONTENT")],
            tenant_id="t1",
        )
        assert qid == 42
        sql, params = mock_run_sql.call_args[0]
        assert "INSERT INTO email_queue" in sql
        assert params["to"] == ["user@test.fr"]
        assert params["subj"] == "Hello"
        assert "PDF-CONTENT" in base64.b64decode(
            json.loads(params["att"])[0]["content_b64"]
        ).decode()

    @patch("common.email_queue.run_sql", side_effect=OSError("DB down"))
    def test_db_error_returns_none_not_raise(self, _mock):
        qid = enqueue(
            to_emails=["a@b.fr"],
            subject="x",
            html_body="y",
        )
        assert qid is None


class TestRetryPendingEmails:
    @patch("common.email_queue.run_sql")
    @patch("common.email.send_html_with_pdf")
    def test_sends_pending_and_marks_sent(self, mock_send, mock_run_sql):
        # list_pending returns 1 row, then mark_sent UPDATE returns empty
        mock_run_sql.side_effect = [
            [{
                "id": 1,
                "tenant_id": "t1",
                "to_emails": ["a@b.fr"],
                "cc_emails": [],
                "subject": "x",
                "html_body": "body",
                "attachments": [],
                "reply_to": None,
                "attempts": 0,
            }],
            [],  # mark_sent UPDATE
        ]
        mock_send.return_value = {"provider_msg_id": "msg-123"}

        summary = retry_pending_emails(batch_size=5)
        assert summary["attempted"] == 1
        assert summary["sent"] == 1
        assert summary["retried"] == 0
        assert summary["failed"] == 0
        mock_send.assert_called_once()

    @patch("common.email_queue.run_sql")
    @patch("common.email.send_html_with_pdf")
    def test_retry_on_transient_error(self, mock_send, mock_run_sql):
        from common.email import EmailSendError
        mock_run_sql.side_effect = [
            [{
                "id": 2,
                "tenant_id": "t1",
                "to_emails": ["a@b.fr"],
                "cc_emails": [],
                "subject": "x",
                "html_body": "body",
                "attachments": [],
                "reply_to": None,
                "attempts": 1,  # déjà 1 tentative
            }],
            [],  # mark_retry UPDATE
        ]
        mock_send.side_effect = EmailSendError("Brevo 502 Bad Gateway")
        summary = retry_pending_emails()
        assert summary["retried"] == 1
        assert summary["failed"] == 0

    @patch("common.email_queue.run_sql")
    @patch("common.email.send_html_with_pdf")
    def test_marks_failed_after_max_attempts(self, mock_send, mock_run_sql):
        from common.email import EmailSendError
        mock_run_sql.side_effect = [
            [{
                "id": 3,
                "tenant_id": "t1",
                "to_emails": ["a@b.fr"],
                "cc_emails": [],
                "subject": "x",
                "html_body": "body",
                "attachments": [],
                "reply_to": None,
                "attempts": MAX_ATTEMPTS - 1,  # +1 dans la boucle → >= MAX
            }],
            [],  # mark_retry UPDATE
        ]
        mock_send.side_effect = EmailSendError("still failing")
        summary = retry_pending_emails()
        assert summary["failed"] == 1
        assert summary["retried"] == 0


class TestSendWithQueueFallback:
    @patch("common.email.send_html_with_pdf")
    def test_success_does_not_enqueue(self, mock_send):
        mock_send.return_value = {"status": "sent", "provider_msg_id": "msg-1"}
        with patch("common.email_queue.enqueue") as mock_enq:
            result = send_with_queue_fallback(
                to_email="a@b.fr",
                subject="Hi",
                html_body="Body",
            )
        assert result["status"] == "sent"
        mock_enq.assert_not_called()

    @patch("common.email.send_html_with_pdf")
    def test_failure_falls_back_to_queue(self, mock_send):
        from common.email import EmailSendError
        mock_send.side_effect = EmailSendError("Brevo 503")
        with patch("common.email_queue.enqueue", return_value=99) as mock_enq:
            result = send_with_queue_fallback(
                to_email=["a@b.fr", "c@d.fr"],
                subject="Hi",
                html_body="Body",
                cc=["cc@e.fr"],
            )
        assert result["status"] == "queued"
        assert result["queue_id"] == 99
        mock_enq.assert_called_once()
        # Vérifier que to_emails est bien une liste
        call_kwargs = mock_enq.call_args.kwargs
        assert call_kwargs["to_emails"] == ["a@b.fr", "c@d.fr"]
        assert call_kwargs["cc_emails"] == ["cc@e.fr"]
        assert call_kwargs["last_error"] == "Brevo 503"

    @patch("common.email.send_html_with_pdf")
    def test_queue_failure_reraises_original(self, mock_send):
        from common.email import EmailSendError
        mock_send.side_effect = EmailSendError("Brevo 503")
        # enqueue returns None → réelève l'erreur
        with patch("common.email_queue.enqueue", return_value=None):
            import pytest
            with pytest.raises(EmailSendError):
                send_with_queue_fallback(
                    to_email="a@b.fr",
                    subject="Hi",
                    html_body="Body",
                )
