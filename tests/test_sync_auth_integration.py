"""Tests d'intégration pour _verify_sync_auth (API key + rate-limit).

Mock les deux dépendances externes (verify_api_key + state du rate limiter)
pour vérifier le flow complet : extraction bearer → vérif clé → check quota.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from common.sync import rate_limit


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    rate_limit.reset()
    yield
    rate_limit.reset()


def _fake_request(authorization: str | None = None) -> SimpleNamespace:
    """Stand-in minimal pour starlette.Request (seule .headers est utilisée)."""
    headers = {}
    if authorization is not None:
        headers["authorization"] = authorization
    return SimpleNamespace(headers=headers)


class TestVerifySyncAuth:

    def test_missing_authorization_returns_401(self):
        from app_nicegui import _verify_sync_auth
        auth_info, err = _verify_sync_auth(_fake_request(None))
        assert auth_info is None
        assert err is not None
        assert err.status_code == 401
        body = json.loads(bytes(err.body).decode())
        assert "Missing Authorization" in body["error"]

    def test_invalid_bearer_format_returns_401(self):
        from app_nicegui import _verify_sync_auth
        # Pas de "Bearer " devant
        auth_info, err = _verify_sync_auth(_fake_request("Basic foo"))
        assert auth_info is None
        assert err.status_code == 401

    @patch("common.sync.api_key.verify_api_key", return_value=None)
    def test_invalid_api_key_returns_401(self, _mock_verify):
        from app_nicegui import _verify_sync_auth
        auth_info, err = _verify_sync_auth(_fake_request("Bearer bogus-key"))
        assert auth_info is None
        assert err.status_code == 401
        body = json.loads(bytes(err.body).decode())
        assert body["error"] == "Invalid API key"

    @patch("common.sync.api_key.verify_api_key")
    def test_valid_key_returns_auth_info(self, mock_verify):
        from app_nicegui import _verify_sync_auth
        mock_verify.return_value = {"tenant_id": "t1", "key_id": "k1"}
        auth_info, err = _verify_sync_auth(_fake_request("Bearer good-key"))
        assert err is None
        assert auth_info == {"tenant_id": "t1", "key_id": "k1"}

    @patch("common.sync.api_key.verify_api_key")
    def test_rate_limit_triggers_429_after_threshold(self, mock_verify):
        from app_nicegui import _verify_sync_auth
        mock_verify.return_value = {"tenant_id": "t1", "key_id": "saturation-key"}
        # Atteindre la limite par défaut (60) en bouclant
        allowed_count = 0
        for _ in range(65):
            info, err = _verify_sync_auth(_fake_request("Bearer good-key"))
            if err is None:
                allowed_count += 1
            else:
                assert err.status_code == 429
                assert "Retry-After" in err.headers
                body = json.loads(bytes(err.body).decode())
                assert body["error"] == "Rate limit exceeded"
                assert body["retry_after"] > 0
                break
        assert allowed_count == 60  # toutes les premières sont passées

    @patch("common.sync.api_key.verify_api_key")
    def test_rate_limit_scoped_per_key(self, mock_verify):
        """Une clé saturée ne doit pas bloquer une autre clé."""
        from app_nicegui import _verify_sync_auth

        # Saturation clé 1
        mock_verify.return_value = {"tenant_id": "t1", "key_id": "key-A"}
        for _ in range(60):
            _verify_sync_auth(_fake_request("Bearer A"))

        # Une requête de plus sur key-A → 429
        mock_verify.return_value = {"tenant_id": "t1", "key_id": "key-A"}
        _, err_a = _verify_sync_auth(_fake_request("Bearer A"))
        assert err_a is not None and err_a.status_code == 429

        # Clé B intacte → passe
        mock_verify.return_value = {"tenant_id": "t2", "key_id": "key-B"}
        info_b, err_b = _verify_sync_auth(_fake_request("Bearer B"))
        assert err_b is None
        assert info_b["tenant_id"] == "t2"
