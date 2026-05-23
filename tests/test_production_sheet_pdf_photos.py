"""Tests for common/production_sheet_pdf.py — _load_photo_bytes (S3 + legacy)."""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

from common.production_sheet_pdf import _fallback_base64, _load_photo_bytes

# ─── _fallback_base64 ─────────────────────────────────────────────────────


class TestFallbackBase64:

    def test_decodes_plain_base64(self):
        raw = b"hello"
        b64 = base64.b64encode(raw).decode()
        assert _fallback_base64({"base64": b64}) == raw

    def test_decodes_data_url(self):
        raw = b"jpeg-bytes"
        b64 = base64.b64encode(raw).decode()
        url = f"data:image/jpeg;base64,{b64}"
        assert _fallback_base64({"base64": url}) == raw

    def test_returns_none_if_no_base64(self):
        assert _fallback_base64({}) is None
        assert _fallback_base64({"base64": ""}) is None

    def test_returns_none_if_invalid_base64(self):
        assert _fallback_base64({"base64": "!!!"}) is None or _fallback_base64(
            {"base64": "!!!"}
        ) == b""


# ─── _load_photo_bytes — formats ──────────────────────────────────────────


class TestLoadPhotoBytes:

    def test_non_dict_returns_none(self):
        assert _load_photo_bytes(None) is None
        assert _load_photo_bytes("string") is None
        assert _load_photo_bytes(123) is None

    def test_legacy_base64_only(self):
        """Pas de key → fallback base64."""
        raw = b"\xff\xd8\xff\xe0fake-jpeg"
        b64 = base64.b64encode(raw).decode()
        result = _load_photo_bytes({"base64": b64})
        assert result == raw

    def test_neither_key_nor_base64_returns_none(self):
        assert _load_photo_bytes({}) is None
        assert _load_photo_bytes({"other_field": "x"}) is None

    @patch("common.object_storage.get_presigned_url")
    @patch("requests.get")
    def test_s3_key_downloads_via_presigned(
        self, mock_get: MagicMock, mock_presigned: MagicMock,
    ):
        """Photo avec key → download via URL signée."""
        mock_presigned.return_value = "https://signed.example.com/key"
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"\xff\xd8\xff\xe0from-s3"
        mock_get.return_value = fake_resp

        result = _load_photo_bytes({"key": "production/photos/t1/2026/s1/abc.jpg"})

        assert result == b"\xff\xd8\xff\xe0from-s3"
        mock_presigned.assert_called_once()
        mock_get.assert_called_once_with(
            "https://signed.example.com/key", timeout=15,
        )

    @patch("common.object_storage.get_presigned_url")
    @patch("requests.get")
    def test_s3_priority_over_base64(
        self, mock_get: MagicMock, mock_presigned: MagicMock,
    ):
        """Si key ET base64 présents, on utilise S3 (source de vérité)."""
        mock_presigned.return_value = "https://signed.example.com/key"
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"\xff\xd8\xff\xe0from-s3"
        mock_get.return_value = fake_resp

        legacy_b64 = base64.b64encode(b"old-legacy").decode()
        result = _load_photo_bytes({
            "key": "production/photos/.../abc.jpg",
            "base64": legacy_b64,
        })
        # Bytes S3, pas du base64 legacy
        assert result == b"\xff\xd8\xff\xe0from-s3"

    @patch("common.object_storage.get_presigned_url")
    def test_s3_presigned_failure_fallbacks_to_base64(
        self, mock_presigned: MagicMock,
    ):
        """Si génération URL signée échoue, on fallback sur base64 si dispo."""
        from common.object_storage import OVHStorageError
        mock_presigned.side_effect = OVHStorageError("S3 down")

        legacy_b64 = base64.b64encode(b"\xff\xd8\xff\xe0fallback").decode()
        result = _load_photo_bytes({
            "key": "production/photos/.../abc.jpg",
            "base64": legacy_b64,
        })
        assert result == b"\xff\xd8\xff\xe0fallback"

    @patch("common.object_storage.get_presigned_url")
    @patch("requests.get")
    def test_s3_http_error_fallbacks_to_base64(
        self, mock_get: MagicMock, mock_presigned: MagicMock,
    ):
        """Si le HTTP renvoie 404/500, on fallback sur base64."""
        mock_presigned.return_value = "https://signed.example.com/key"
        fake_resp = MagicMock()
        fake_resp.status_code = 404
        fake_resp.content = b""
        mock_get.return_value = fake_resp

        legacy_b64 = base64.b64encode(b"backup").decode()
        result = _load_photo_bytes({
            "key": "production/photos/.../abc.jpg",
            "base64": legacy_b64,
        })
        assert result == b"backup"

    @patch("common.object_storage.get_presigned_url")
    @patch("requests.get")
    def test_s3_failure_without_base64_returns_none(
        self, mock_get: MagicMock, mock_presigned: MagicMock,
    ):
        """S3 down + pas de base64 → None (skip cette photo, mais ne crashe pas le PDF)."""
        mock_presigned.return_value = "https://signed.example.com/key"
        mock_get.side_effect = RuntimeError("network")
        result = _load_photo_bytes({"key": "production/photos/.../abc.jpg"})
        assert result is None

    @patch("common.object_storage.get_presigned_url")
    def test_empty_key_falls_through_to_base64(
        self, mock_presigned: MagicMock,
    ):
        """Key vide → on tente base64 directement (pas d'appel S3)."""
        raw_b64 = base64.b64encode(b"\xff\xd8\xff\xe0direct").decode()
        result = _load_photo_bytes({"key": "", "base64": raw_b64})
        assert result == b"\xff\xd8\xff\xe0direct"
        mock_presigned.assert_not_called()
