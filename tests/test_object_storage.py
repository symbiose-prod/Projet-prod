"""Tests for common/object_storage/ovh_s3.py."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common.object_storage.ovh_s3 import (
    OVHStorageError,
    _detect_extension,
    delete_photo,
    generate_photo_key,
    get_presigned_url,
    is_configured,
    upload_photo,
)

# ─── is_configured ────────────────────────────────────────────────────────


class TestIsConfigured:

    @patch.dict("os.environ", {}, clear=True)
    def test_false_when_empty(self):
        assert is_configured() is False

    @patch.dict("os.environ", {
        "OVH_S3_ENDPOINT": "https://s3.gra.io.cloud.ovh.net",
        "OVH_S3_BUCKET": "ferment-photos",
        "OVH_S3_ACCESS_KEY": "key",
        "OVH_S3_SECRET_KEY": "secret",
    })
    def test_true_when_all_present(self):
        assert is_configured() is True

    @patch.dict("os.environ", {
        "OVH_S3_ENDPOINT": "x",
        "OVH_S3_BUCKET": "x",
        # Manque ACCESS_KEY et SECRET_KEY
    }, clear=True)
    def test_false_when_partial(self):
        assert is_configured() is False


# ─── _detect_extension ────────────────────────────────────────────────────


class TestDetectExtension:

    def test_jpeg(self):
        assert _detect_extension("image/jpeg") == "jpg"

    def test_png(self):
        assert _detect_extension("image/png") == "png"

    def test_heic(self):
        assert _detect_extension("image/heic") == "heic"

    def test_unknown_falls_back_to_jpg(self):
        assert _detect_extension("application/octet-stream") == "jpg"
        assert _detect_extension("") == "jpg"

    def test_strips_parameters(self):
        assert _detect_extension("image/png; charset=binary") == "png"


# ─── generate_photo_key ───────────────────────────────────────────────────


class TestGeneratePhotoKey:

    def test_default_extension(self):
        key = generate_photo_key(tenant_id="t1", sheet_id="s1")
        assert key.startswith("production/photos/t1/")
        assert "/s1/" in key
        assert key.endswith(".jpg")

    def test_custom_extension(self):
        key = generate_photo_key(tenant_id="t1", sheet_id="s1", extension="png")
        assert key.endswith(".png")

    def test_extension_with_dot(self):
        key = generate_photo_key(tenant_id="t1", sheet_id="s1", extension=".webp")
        assert key.endswith(".webp")

    def test_keys_are_unique(self):
        k1 = generate_photo_key(tenant_id="t1", sheet_id="s1")
        k2 = generate_photo_key(tenant_id="t1", sheet_id="s1")
        assert k1 != k2  # uuid garantit l'unicité

    @patch.dict("os.environ", {"OVH_S3_PHOTOS_PREFIX": "custom-prefix/"})
    def test_custom_prefix(self):
        key = generate_photo_key(tenant_id="t1", sheet_id="s1")
        assert key.startswith("custom-prefix/t1/")

    @patch.dict("os.environ", {"OVH_S3_PHOTOS_PREFIX": "no-slash"})
    def test_prefix_slash_normalization(self):
        """Si le prefix ne finit pas par /, on l'ajoute."""
        key = generate_photo_key(tenant_id="t1", sheet_id="s1")
        assert key.startswith("no-slash/t1/")


# ─── upload_photo ─────────────────────────────────────────────────────────


_CFG = {
    "OVH_S3_ENDPOINT": "https://s3.test.ovh.net",
    "OVH_S3_BUCKET": "test-bucket",
    "OVH_S3_ACCESS_KEY": "key",
    "OVH_S3_SECRET_KEY": "secret",
    "OVH_S3_REGION": "gra",
}


class TestUploadPhoto:

    def setup_method(self):
        # Reset le singleton client à chaque test pour ne pas garder un mock entre tests
        import common.object_storage.ovh_s3 as mod
        mod._client = None

    def test_raises_on_empty_bytes(self):
        with pytest.raises(OVHStorageError, match="vides"):
            upload_photo(b"", tenant_id="t1", sheet_id="s1")

    def test_raises_on_missing_tenant_or_sheet(self):
        with pytest.raises(OVHStorageError):
            upload_photo(b"x", tenant_id="", sheet_id="s1")
        with pytest.raises(OVHStorageError):
            upload_photo(b"x", tenant_id="t1", sheet_id="")

    @patch.dict("os.environ", {}, clear=True)
    def test_raises_if_not_configured(self):
        with pytest.raises(OVHStorageError, match="non configuré"):
            upload_photo(b"x", tenant_id="t1", sheet_id="s1")

    @patch.dict("os.environ", _CFG)
    @patch("common.object_storage.ovh_s3._import_boto3")
    def test_happy_path(self, mock_import: MagicMock):
        fake_boto3 = MagicMock()
        fake_client = MagicMock()
        fake_boto3.client.return_value = fake_client
        mock_import.return_value = (fake_boto3, MagicMock())

        key = upload_photo(
            b"\xff\xd8\xff\xe0fake-jpeg-bytes",
            tenant_id="tenant-abc",
            sheet_id="sheet-123",
            content_type="image/jpeg",
        )
        assert key.startswith("production/photos/tenant-abc/")
        assert key.endswith(".jpg")

        fake_client.put_object.assert_called_once()
        kwargs = fake_client.put_object.call_args.kwargs
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"] == key
        assert kwargs["ContentType"] == "image/jpeg"
        assert kwargs["Metadata"]["tenant-id"] == "tenant-abc"
        assert kwargs["Metadata"]["sheet-id"] == "sheet-123"

    @patch.dict("os.environ", _CFG)
    @patch("common.object_storage.ovh_s3._import_boto3")
    def test_wraps_boto_errors(self, mock_import: MagicMock):
        fake_boto3 = MagicMock()
        fake_client = MagicMock()
        fake_client.put_object.side_effect = RuntimeError("network down")
        fake_boto3.client.return_value = fake_client
        mock_import.return_value = (fake_boto3, MagicMock())

        with pytest.raises(OVHStorageError, match="upload_photo failed"):
            upload_photo(b"x", tenant_id="t1", sheet_id="s1")


# ─── get_presigned_url ────────────────────────────────────────────────────


class TestGetPresignedUrl:

    def setup_method(self):
        import common.object_storage.ovh_s3 as mod
        mod._client = None

    def test_raises_on_empty_key(self):
        with pytest.raises(OVHStorageError):
            get_presigned_url("")

    def test_raises_on_invalid_ttl(self):
        with pytest.raises(OVHStorageError, match="ttl"):
            get_presigned_url("some-key", ttl_seconds=0)
        with pytest.raises(OVHStorageError, match="ttl"):
            get_presigned_url("some-key", ttl_seconds=86400 * 8)  # > 7 jours

    @patch.dict("os.environ", _CFG)
    @patch("common.object_storage.ovh_s3._import_boto3")
    def test_happy_path(self, mock_import: MagicMock):
        fake_boto3 = MagicMock()
        fake_client = MagicMock()
        fake_client.generate_presigned_url.return_value = "https://signed.example.com/x"
        fake_boto3.client.return_value = fake_client
        mock_import.return_value = (fake_boto3, MagicMock())

        url = get_presigned_url("some-key", ttl_seconds=600)
        assert url == "https://signed.example.com/x"
        kwargs = fake_client.generate_presigned_url.call_args.kwargs
        assert kwargs["ExpiresIn"] == 600
        assert kwargs["Params"]["Key"] == "some-key"


# ─── delete_photo ─────────────────────────────────────────────────────────


class TestDeletePhoto:

    def setup_method(self):
        import common.object_storage.ovh_s3 as mod
        mod._client = None

    def test_returns_false_on_empty_key(self):
        assert delete_photo("") is False

    @patch.dict("os.environ", _CFG)
    @patch("common.object_storage.ovh_s3._import_boto3")
    def test_happy_path(self, mock_import: MagicMock):
        fake_boto3 = MagicMock()
        fake_client = MagicMock()
        fake_boto3.client.return_value = fake_client
        mock_import.return_value = (fake_boto3, MagicMock())
        assert delete_photo("some-key") is True
        fake_client.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key="some-key",
        )

    @patch.dict("os.environ", _CFG)
    @patch("common.object_storage.ovh_s3._import_boto3")
    def test_returns_false_on_boto_error(self, mock_import: MagicMock):
        fake_boto3 = MagicMock()
        fake_client = MagicMock()
        fake_client.delete_object.side_effect = RuntimeError("nope")
        fake_boto3.client.return_value = fake_client
        mock_import.return_value = (fake_boto3, MagicMock())
        # Best-effort : pas d'exception levée
        assert delete_photo("some-key") is False
