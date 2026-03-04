"""Tests for common/auth.py — validation, hashing, and lockout logic."""
from __future__ import annotations

import pytest

from common.auth import (
    MIN_PASSWORD_LENGTH,
    _lockout_seconds_for,
    hash_password,
    validate_email,
    validate_password,
    verify_password,
)

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

    def test_min_length_from_config(self):
        """MIN_PASSWORD_LENGTH est chargé depuis config.yaml (default 10)."""
        assert MIN_PASSWORD_LENGTH >= 8

    def test_valid_password(self):
        assert validate_password("MySecure80") == "MySecure80"

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="caractères"):
            validate_password("short1")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="caractères"):
            validate_password("")

    def test_all_digits_raises(self):
        with pytest.raises(ValueError, match="lettre et un chiffre"):
            validate_password("1234567890")

    def test_all_letters_raises(self):
        with pytest.raises(ValueError, match="lettre et un chiffre"):
            validate_password("abcdefghij")

    def test_letters_and_digits_ok(self):
        assert validate_password("abcdefgh12") == "abcdefgh12"

    def test_special_chars_with_letter_and_digit_ok(self):
        assert validate_password("P@ssw0rd!!") == "P@ssw0rd!!"

    def test_exact_min_length_ok(self):
        pwd = "a" * (MIN_PASSWORD_LENGTH - 1) + "1"
        assert validate_password(pwd) == pwd

    def test_one_below_min_length_raises(self):
        pwd = "a" * (MIN_PASSWORD_LENGTH - 2) + "1"
        with pytest.raises(ValueError, match="caractères"):
            validate_password(pwd)


# ─── hash_password + verify_password roundtrip ───────────────────────────────


class TestPasswordRoundtrip:

    def test_correct_password_verifies(self):
        hashed = hash_password("MySecure80")
        assert verify_password("MySecure80", hashed) is True

    def test_wrong_password_fails(self):
        hashed = hash_password("MySecure80")
        assert verify_password("WrongPassword1", hashed) is False

    def test_hash_format(self):
        hashed = hash_password("test12345X")
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


# ─── lockout exponentiel ─────────────────────────────────────────────────────


class TestLockoutSeconds:

    def test_below_first_threshold_returns_zero(self):
        assert _lockout_seconds_for(0) == 0
        assert _lockout_seconds_for(4) == 0

    def test_first_threshold_5_failures(self):
        assert _lockout_seconds_for(5) == 300

    def test_between_thresholds(self):
        assert _lockout_seconds_for(7) == 300

    def test_second_threshold_10_failures(self):
        assert _lockout_seconds_for(10) == 1800

    def test_third_threshold_15_failures(self):
        assert _lockout_seconds_for(15) == 7200

    def test_above_max_threshold(self):
        """Au-delà du palier le plus haut, on reste au max."""
        assert _lockout_seconds_for(100) == 7200
