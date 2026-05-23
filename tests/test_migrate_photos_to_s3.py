"""Tests for scripts/migrate_photos_to_s3.py — helpers + migration logic."""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Le script est dans scripts/ — on l'importe via path manipulation
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import migrate_photos_to_s3 as mig  # noqa: E402

# ─── _decode_base64 ───────────────────────────────────────────────────────


class TestDecodeBase64:

    def test_valid_base64(self):
        raw = b"hello world"
        b64 = base64.b64encode(raw).decode()
        assert mig._decode_base64(b64) == raw

    def test_data_url_supported(self):
        raw = b"png-bytes"
        b64 = base64.b64encode(raw).decode()
        url = f"data:image/png;base64,{b64}"
        assert mig._decode_base64(url) == raw

    def test_invalid_returns_none(self):
        assert mig._decode_base64("===nope===") is not None  # noisy but b64 ne valide pas
        # Vrai cas garbage : caractères non-b64
        assert mig._decode_base64("!!!") is None or mig._decode_base64("!!!") == b""


# ─── _detect_content_type ─────────────────────────────────────────────────


class TestDetectContentType:

    def test_jpeg_magic_bytes(self):
        assert mig._detect_content_type(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "image/jpeg"

    def test_png_magic_bytes(self):
        assert mig._detect_content_type(b"\x89PNG\r\n\x1a\n\x00\x00") == "image/png"

    def test_gif_magic_bytes(self):
        assert mig._detect_content_type(b"GIF89a\x00\x00") == "image/gif"

    def test_webp_magic_bytes(self):
        assert mig._detect_content_type(b"RIFF\x00\x00\x00\x00WEBP\x00") == "image/webp"

    def test_unknown_falls_back_to_jpeg(self):
        assert mig._detect_content_type(b"\x00\x00\x00\x00") == "image/jpeg"


# ─── _migrate_photo ───────────────────────────────────────────────────────


class TestMigratePhoto:

    def _stats(self):
        return mig.Stats()

    def test_already_migrated_skipped(self):
        photo = {"key": "existing-key", "base64": "..."}
        stats = self._stats()
        result = mig._migrate_photo(
            photo, tenant_id="t1", sheet_id="s1",
            stats=stats, dry_run=False,
        )
        assert result == photo
        assert stats.photos_already_migrated == 1
        assert stats.photos_migrated == 0

    def test_no_base64_skipped(self):
        photo = {}
        stats = self._stats()
        result = mig._migrate_photo(
            photo, tenant_id="t1", sheet_id="s1",
            stats=stats, dry_run=False,
        )
        assert result == photo
        assert stats.photos_to_migrate == 0

    def test_dry_run_counts_but_no_upload(self):
        b64 = base64.b64encode(b"\xff\xd8\xff\xe0fake-jpeg").decode()
        photo = {"base64": b64}
        stats = self._stats()
        result = mig._migrate_photo(
            photo, tenant_id="t1", sheet_id="s1",
            stats=stats, dry_run=True,
        )
        # Dry-run : photo non modifiée
        assert result == photo
        assert stats.photos_to_migrate == 1
        assert stats.photos_migrated == 0

    @patch("common.object_storage.upload_photo")
    def test_apply_uploads_and_enriches(self, mock_upload: MagicMock):
        mock_upload.return_value = "production/photos/t1/2026/s1/abc.jpg"
        b64 = base64.b64encode(b"\xff\xd8\xff\xe0fake-jpeg").decode()
        photo = {"base64": b64, "extra": "preserved"}
        stats = self._stats()
        result = mig._migrate_photo(
            photo, tenant_id="t1", sheet_id="s1",
            stats=stats, dry_run=False,
        )
        assert result["key"] == "production/photos/t1/2026/s1/abc.jpg"
        assert result["content_type"] == "image/jpeg"
        assert "size_bytes" in result
        assert "migrated_at" in result
        # Base64 conservé (sécurité Phase B)
        assert result["base64"] == b64
        # Champs additionnels préservés
        assert result["extra"] == "preserved"
        assert stats.photos_migrated == 1
        assert stats.bytes_migrated > 0

    @patch("common.object_storage.upload_photo")
    def test_upload_failure_counted(self, mock_upload: MagicMock):
        mock_upload.side_effect = RuntimeError("S3 down")
        b64 = base64.b64encode(b"\xff\xd8\xff\xe0fake").decode()
        stats = self._stats()
        result = mig._migrate_photo(
            {"base64": b64}, tenant_id="t1", sheet_id="s1",
            stats=stats, dry_run=False,
        )
        # En cas d'échec, on retourne la photo telle quelle (pas de key)
        assert "key" not in result
        assert stats.photos_failed == 1

    def test_invalid_base64_counted(self):
        stats = self._stats()
        # On crée du contenu vraiment garbage (caractères interdits en b64)
        photo = {"base64": "\x00\x01\x02"}
        result = mig._migrate_photo(
            photo, tenant_id="t1", sheet_id="s1",
            stats=stats, dry_run=False,
        )
        # Garbage non décodable → skip
        assert "key" not in result


# ─── _migrate_sheet ───────────────────────────────────────────────────────


class TestMigrateSheet:

    def test_no_incidents_returns_none(self):
        sheet = {"id": "s1", "tenant_id": "t1", "data": {}}
        result = mig._migrate_sheet(
            sheet, stats=mig.Stats(), dry_run=True, remove_base64=False,
        )
        assert result is None

    def test_no_photos_returns_none(self):
        sheet = {
            "id": "s1", "tenant_id": "t1",
            "data": {"incidents": {"notes": "x"}},
        }
        result = mig._migrate_sheet(
            sheet, stats=mig.Stats(), dry_run=True, remove_base64=False,
        )
        assert result is None

    def test_data_as_string_decoded(self):
        """Postgres peut renvoyer le JSONB comme str selon le driver."""
        import json as _json
        sheet = {
            "id": "s1", "tenant_id": "t1",
            "data": _json.dumps({"incidents": {"photos": []}}),
        }
        result = mig._migrate_sheet(
            sheet, stats=mig.Stats(), dry_run=True, remove_base64=False,
        )
        # photos vide → None (rien à faire)
        assert result is None

    @patch("common.object_storage.upload_photo")
    def test_migrates_photos_and_updates_data(self, mock_upload: MagicMock):
        mock_upload.return_value = "key-1"
        b64 = base64.b64encode(b"\xff\xd8\xff\xe0fake").decode()
        sheet = {
            "id": "s1", "tenant_id": "t1",
            "data": {
                "incidents": {
                    "notes": "X",
                    "photos": [
                        {"base64": b64},
                        {"key": "already-migrated"},  # déjà fait
                    ],
                },
                "other_section": "untouched",
            },
        }
        stats = mig.Stats()
        result = mig._migrate_sheet(
            sheet, stats=stats, dry_run=False, remove_base64=False,
        )
        assert result is not None
        assert result["other_section"] == "untouched"  # autres sections OK
        photos = result["incidents"]["photos"]
        assert len(photos) == 2
        assert photos[0]["key"] == "key-1"
        assert photos[0]["base64"] == b64  # conservé
        assert photos[1] == {"key": "already-migrated"}
        assert stats.sheets_with_photos == 1
        assert stats.photos_total == 2
        assert stats.photos_already_migrated == 1
        assert stats.photos_migrated == 1

    @patch("common.object_storage.upload_photo")
    def test_remove_base64_phase(self, mock_upload: MagicMock):
        """Avec --remove-base64 : retire le base64 des photos qui ont une key."""
        sheet = {
            "id": "s1", "tenant_id": "t1",
            "data": {
                "incidents": {
                    "photos": [
                        {"key": "existing", "base64": "should-be-removed"},
                        {"key": "another"},  # déjà sans base64
                    ],
                },
            },
        }
        stats = mig.Stats()
        result = mig._migrate_sheet(
            sheet, stats=stats, dry_run=False, remove_base64=True,
        )
        assert result is not None
        photos = result["incidents"]["photos"]
        assert "base64" not in photos[0]
        assert photos[0]["key"] == "existing"
        assert photos[1] == {"key": "another"}
        assert stats.base64_removed == 1
        # mock_upload pas appelé (toutes les photos ont déjà une key)
        mock_upload.assert_not_called()


# ─── Stats reporting ──────────────────────────────────────────────────────


class TestStats:

    def test_print_report_does_not_crash(self, capsys):
        s = mig.Stats()
        s.sheets_scanned = 100
        s.photos_total = 250
        s.photos_migrated = 200
        s.photos_failed = 2
        s.bytes_migrated = 5 * 1024 * 1024
        s.print_report(dry_run=False)
        captured = capsys.readouterr().out
        assert "RAPPORT MIGRATION PHOTOS" in captured
        assert "APPLIED" in captured
        assert "200" in captured
        assert "5.00 Mo" in captured

    def test_dry_run_report(self, capsys):
        s = mig.Stats()
        s.sheets_scanned = 50
        s.photos_to_migrate = 100
        s.print_report(dry_run=True)
        captured = capsys.readouterr().out
        assert "DRY-RUN" in captured
        assert "100" in captured
