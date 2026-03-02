"""Tests for common/auth.py — pure validation and hashing functions."""
from __future__ import annotations

import pytest

from common.auth import validate_email, validate_password, hash_password, verify_password


# ─── validate_email ───────────────────────────────────────────────────────────

class TestValidateEmail:

    def test_valid_email(self):
        assert validate_email("user@example.com") == "user@example.com"

    def test_strips_whitespace(self):
        assert validate_email("  user@example.com  ") == "user@example.com"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="invalide"):
            validate_email("")

    def test_none_raises(self):
        with pytest.raises(ValueError, match="invalide"):
            validate_email(None)

    def test_no_at_raises(self):
        with pytest.raises(ValueError, match="invalide"):
            validate_email("userexample.com")

    def test_double_dots_raises(self):
        with pytest.raises(ValueError, match="points consécutifs"):
            validate_email("user..name@example.com")

    def test_too_long_raises(self):
        long_email = "a" * 250 + "@b.com"
        with pytest.raises(ValueError, match="trop longue"):
            validate_email(long_email)

    def test_local_part_too_long_raises(self):
        local = "a" * 65
        with pytest.raises(ValueError, match="Partie locale"):
            validate_email(f"{local}@example.com")

    def test_valid_with_dots_and_plus(self):
        assert validate_email("first.last+tag@example.co.uk") == "first.last+tag@example.co.uk"

    def test_single_char_domain(self):
        # 2-char TLD minimum
        with pytest.raises(ValueError, match="invalide"):
            validate_email("user@example.c")


# ─── validate_password ────────────────────────────────────────────────────────

class TestValidatePassword:

    def test_valid_password(self):
        assert validate_password("MySecure8!") == "MySecure8!"

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="8 caractères"):
            validate_password("short")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="8 caractères"):
            validate_password("")

    def test_all_digits_raises(self):
        with pytest.raises(ValueError, match="uniquement des chiffres"):
            validate_password("12345678")

    def test_eight_chars_ok(self):
        assert validate_password("abcdefg1") == "abcdefg1"


# ─── hash_password + verify_password roundtrip ───────────────────────────────

class TestPasswordRoundtrip:

    def test_correct_password_verifies(self):
        hashed = hash_password("MySecure8!")
        assert verify_password("MySecure8!", hashed) is True

    def test_wrong_password_fails(self):
        hashed = hash_password("MySecure8!")
        assert verify_password("WrongPassword", hashed) is False

    def test_hash_format(self):
        hashed = hash_password("test1234")
        parts = hashed.split("$")
        assert len(parts) == 4
        assert parts[0] == "pbkdf2_sha256"
        assert parts[1] == "310000"

    def test_different_hashes_for_same_password(self):
        # Salt randomness → different hashes
        h1 = hash_password("same_password1")
        h2 = hash_password("same_password1")
        assert h1 != h2
        assert verify_password("same_password1", h1)
        assert verify_password("same_password1", h2)

    def test_empty_password_raises(self):
        with pytest.raises(ValueError):
            hash_password("")

    def test_verify_garbage_returns_false(self):
        assert verify_password("anything", "garbage_hash") is False
