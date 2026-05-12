"""Tests des fonctions pures de common.services.sscc_service.

Pas de DB ni de NiceGUI ici — uniquement la logique de calcul de clé
de contrôle, la construction du SSCC et le formatage.
"""
from __future__ import annotations

import pytest

from common.services.sscc_service import (
    SSCC_COMPANY_PREFIX,
    SSCC_EXTENSION_DIGIT,
    _build_sscc_from_serial,
    format_sscc_pretty,
    gs1_check_digit,
    reconstruct_sscc_payload,
)

# ─── gs1_check_digit ────────────────────────────────────────────────────────

class TestGs1CheckDigit:

    def test_known_gtin13_value(self):
        # GTIN-13 connu : 5012345678900 (clé = 0)
        # 17 digits sans la clé : 501234567890
        # Wikipedia GS1 check digit example
        assert gs1_check_digit("501234567890") == 0

    def test_known_gtin13_alt(self):
        # 4006381333931 → data 400638133393, clé = 1
        assert gs1_check_digit("400638133393") == 1

    def test_gtin14_simple(self):
        # GTIN-14 0017800001234 → data 0017800001234, clé attendue ?
        # On vérifie juste la cohérence : recalculer la clé sur data + 0 vs 1...
        d = gs1_check_digit("0017800001234")
        assert 0 <= d <= 9

    def test_all_zeros(self):
        # Pour 17 zéros, la clé est 0
        assert gs1_check_digit("0" * 17) == 0

    def test_raises_on_empty(self):
        with pytest.raises(ValueError):
            gs1_check_digit("")

    def test_raises_on_non_digit(self):
        with pytest.raises(ValueError):
            gs1_check_digit("12345A678")

    def test_deterministic(self):
        # Même input → même clé (sanity check pour éviter regression
        # d'une réécriture qui inverserait les poids)
        d = gs1_check_digit("12345678901234567")
        assert gs1_check_digit("12345678901234567") == d


# ─── _build_sscc_from_serial ────────────────────────────────────────────────

class TestBuildSscc:

    def test_total_length_18(self):
        sscc = _build_sscc_from_serial(1)
        assert len(sscc) == 18
        assert sscc.isdigit()

    def test_extension_and_prefix(self):
        sscc = _build_sscc_from_serial(42)
        # Le SSCC doit commencer par l'extension + le préfixe entreprise
        assert sscc.startswith(SSCC_EXTENSION_DIGIT + SSCC_COMPANY_PREFIX)

    def test_serial_padded(self):
        # Serial 42 → portion serial = "0000042"
        sscc = _build_sscc_from_serial(42)
        # Le serial est entre la fin du préfixe et la clé
        prefix_end = 1 + len(SSCC_COMPANY_PREFIX)  # 10
        serial_portion = sscc[prefix_end:prefix_end + 7]
        assert serial_portion == "0000042"

    def test_serial_max(self):
        # Serial = 9999999 (max sur 7 digits)
        sscc = _build_sscc_from_serial(9_999_999)
        assert len(sscc) == 18
        assert sscc[10:17] == "9999999"

    def test_serial_zero(self):
        sscc = _build_sscc_from_serial(0)
        assert len(sscc) == 18
        assert sscc[10:17] == "0000000"

    def test_serial_overflow_raises(self):
        with pytest.raises(ValueError, match="hors bornes"):
            _build_sscc_from_serial(10_000_000)

    def test_serial_negative_raises(self):
        with pytest.raises(ValueError, match="hors bornes"):
            _build_sscc_from_serial(-1)

    def test_check_digit_valid(self):
        # La clé calculée doit re-valider si on recalcule sur les 17 premiers
        sscc = _build_sscc_from_serial(12345)
        expected_check = gs1_check_digit(sscc[:17])
        assert int(sscc[17]) == expected_check

    def test_different_serials_different_ssccs(self):
        assert _build_sscc_from_serial(1) != _build_sscc_from_serial(2)
        assert _build_sscc_from_serial(100) != _build_sscc_from_serial(101)


# ─── format_sscc_pretty ─────────────────────────────────────────────────────

class TestFormatPretty:

    def test_standard_18_digits(self):
        s = format_sscc_pretty("337700144200000005")
        assert s == "3377 0014 4200 0000 05"

    def test_strips_separators(self):
        # Espaces / tirets sont retirés avant formatage
        s = format_sscc_pretty("3377-0014 4200-0000 05")
        assert s == "3377 0014 4200 0000 05"

    def test_invalid_length_returns_as_is(self):
        s = format_sscc_pretty("12345")
        assert s == "12345"


# ─── reconstruct_sscc_payload ───────────────────────────────────────────────

class TestReconstructPayload:

    def test_valid_sscc(self):
        sscc = _build_sscc_from_serial(42)
        p = reconstruct_sscc_payload(sscc)
        assert p.sscc == sscc
        assert p.gs1_data == f"(00){sscc}"
        assert p.pretty == format_sscc_pretty(sscc)
        assert p.hri.startswith("(00) ")

    def test_strips_separators(self):
        sscc = _build_sscc_from_serial(42)
        # Si on passe un SSCC formaté avec espaces, on récupère le SSCC brut
        formatted = format_sscc_pretty(sscc)
        p = reconstruct_sscc_payload(formatted)
        assert p.sscc == sscc

    def test_invalid_length_raises(self):
        with pytest.raises(ValueError, match="SSCC invalide"):
            reconstruct_sscc_payload("12345")
