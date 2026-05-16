"""
tests/test_mobile_auth.py
=========================
Tests pour ``common/mobile_auth.py`` — gestion des tokens Bearer mobile.

Couvre :
  - ``extract_bearer_token`` (parsing du header Authorization)
  - ``create_mobile_token`` (génération + INSERT DB + retour token+expiry)
  - ``verify_mobile_token`` (validation + mise à jour last_used_at)
  - ``revoke_mobile_token`` (révocation idempotente)
  - hashing SHA-256 (jamais stocké en clair)

Toutes les fonctions DB-dépendantes mockent ``run_sql`` via patch.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from unittest.mock import MagicMock, patch

from common.mobile_auth import (
    MOBILE_TOKEN_TTL_DAYS,
    create_mobile_token,
    extract_bearer_token,
    revoke_mobile_token,
    verify_mobile_token,
)

# ─── extract_bearer_token (pure function) ──────────────────────────────────

class TestExtractBearerToken:
    def test_returns_none_if_header_missing(self):
        assert extract_bearer_token(None) is None
        assert extract_bearer_token("") is None

    def test_returns_none_if_no_bearer_scheme(self):
        assert extract_bearer_token("Basic dGVzdA==") is None
        assert extract_bearer_token("Token xyz") is None

    def test_case_insensitive_scheme(self):
        assert extract_bearer_token("bearer abc123") == "abc123"
        assert extract_bearer_token("BEARER abc123") == "abc123"

    def test_extracts_simple_token(self):
        assert extract_bearer_token("Bearer abc123") == "abc123"

    def test_strips_surrounding_whitespace(self):
        assert extract_bearer_token("  Bearer abc123  ") == "abc123"

    def test_returns_none_if_token_empty(self):
        assert extract_bearer_token("Bearer ") is None
        assert extract_bearer_token("Bearer    ") is None

    def test_returns_none_if_malformed(self):
        # Pas d'espace entre schéma et valeur
        assert extract_bearer_token("Bearerabc") is None


# ─── create_mobile_token ───────────────────────────────────────────────────

class TestCreateMobileToken:
    @patch("common.mobile_auth.run_sql")
    def test_returns_token_and_expires_at(self, mock_sql: MagicMock):
        token, expires_at = create_mobile_token("user-1", "tenant-1")
        assert isinstance(token, str)
        assert len(token) > 30  # secrets.token_urlsafe(32) = ~43 chars
        assert isinstance(expires_at, _dt.datetime)
        # TTL ≈ 90 jours (avec tolérance d'une seconde pour le now())
        delta = expires_at - _dt.datetime.now(_dt.UTC)
        assert abs(delta.total_seconds() - MOBILE_TOKEN_TTL_DAYS * 86400) < 5

    @patch("common.mobile_auth.run_sql")
    def test_inserts_hash_not_plaintext_token(self, mock_sql: MagicMock):
        token, _ = create_mobile_token("user-1", "tenant-1", "iPhone Test")
        # Vérifie qu'on a appelé INSERT avec le hash, pas le token en clair
        call_args = mock_sql.call_args
        params = call_args[0][1]
        assert params["h"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert params["h"] != token

    @patch("common.mobile_auth.run_sql")
    def test_persists_device_name_trimmed_and_clipped(self, mock_sql: MagicMock):
        # Trim
        create_mobile_token("u", "t", "  iPhone Nicolas  ")
        assert mock_sql.call_args[0][1]["d"] == "iPhone Nicolas"
        # Clip à 120 chars (anti-bourrage)
        long_name = "X" * 200
        create_mobile_token("u", "t", long_name)
        assert len(mock_sql.call_args[0][1]["d"]) == 120

    @patch("common.mobile_auth.run_sql")
    def test_custom_ttl(self, mock_sql: MagicMock):
        _, expires_at = create_mobile_token("u", "t", ttl_days=7)
        delta = expires_at - _dt.datetime.now(_dt.UTC)
        # ~7 jours
        assert abs(delta.total_seconds() - 7 * 86400) < 5


# ─── verify_mobile_token ───────────────────────────────────────────────────

class TestVerifyMobileToken:
    def test_returns_none_for_empty(self):
        assert verify_mobile_token("") is None
        assert verify_mobile_token(None) is None  # type: ignore[arg-type]
        assert verify_mobile_token(123) is None  # type: ignore[arg-type]

    @patch("common.mobile_auth.run_sql")
    def test_returns_none_if_not_found(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        assert verify_mobile_token("any-token") is None

    @patch("common.mobile_auth.run_sql")
    def test_returns_user_dict_if_valid(self, mock_sql: MagicMock):
        mock_sql.return_value = [{
            "id": "user-1",
            "tenant_id": "tenant-1",
            "email": "nicolas@test.fr",
            "role": "admin",
            "token_id": "token-uuid-1",
        }]
        result = verify_mobile_token("good-token")
        assert result is not None
        assert result["id"] == "user-1"
        assert result["tenant_id"] == "tenant-1"
        assert result["email"] == "nicolas@test.fr"
        assert result["role"] == "admin"
        # token_id ne doit PAS être exposé (interne)
        assert "token_id" not in result

    @patch("common.mobile_auth.run_sql")
    def test_query_filters_by_hash_not_plaintext(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        verify_mobile_token("plaintext-token")
        # 1er call = SELECT, params doivent contenir le hash
        first_call = mock_sql.call_args_list[0]
        params = first_call[0][1]
        assert params["h"] == hashlib.sha256(b"plaintext-token").hexdigest()

    @patch("common.mobile_auth.run_sql")
    def test_touch_last_used_at_on_success(self, mock_sql: MagicMock):
        mock_sql.return_value = [{
            "id": "u", "tenant_id": "t", "email": "e", "role": "r",
            "token_id": "tid-42",
        }]
        verify_mobile_token("any-token")
        # 2 calls : SELECT puis UPDATE last_used_at
        assert mock_sql.call_count == 2
        update_call = mock_sql.call_args_list[1]
        assert "UPDATE mobile_api_tokens" in update_call[0][0]
        assert update_call[0][1]["id"] == "tid-42"

    @patch("common.mobile_auth.run_sql")
    def test_touch_failure_does_not_break_verify(self, mock_sql: MagicMock):
        # 1er call OK (SELECT), 2ème call raise (UPDATE)
        def side_effect(*args, **kwargs):
            if "UPDATE" in args[0]:
                raise RuntimeError("DB hiccup on touch")
            return [{
                "id": "u", "tenant_id": "t", "email": "e", "role": "r",
                "token_id": "tid",
            }]
        mock_sql.side_effect = side_effect
        # Doit retourner le user, malgré l'échec du UPDATE
        result = verify_mobile_token("good-token")
        assert result is not None
        assert result["id"] == "u"


# ─── revoke_mobile_token ───────────────────────────────────────────────────

class TestRevokeMobileToken:
    def test_returns_false_if_empty(self):
        assert revoke_mobile_token("") is False

    @patch("common.mobile_auth.run_sql")
    def test_returns_true_if_revoked(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"id": "tid-1"}]
        assert revoke_mobile_token("good-token") is True

    @patch("common.mobile_auth.run_sql")
    def test_returns_false_if_already_revoked(self, mock_sql: MagicMock):
        mock_sql.return_value = []  # UPDATE … RETURNING vide = rien à révoquer
        assert revoke_mobile_token("token-already-revoked") is False

    @patch("common.mobile_auth.run_sql")
    def test_query_filters_by_hash(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"id": "x"}]
        revoke_mobile_token("plaintext")
        params = mock_sql.call_args[0][1]
        assert params["h"] == hashlib.sha256(b"plaintext").hexdigest()
