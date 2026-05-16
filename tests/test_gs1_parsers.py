"""
tests/test_gs1_parsers.py
=========================
Tests pour les parsers GS1-128 du service ``etiquette_palette_service``.

Ces parsers sont **critiques** parce qu'ils sont le seul lien entre le code
scanné côté iPhone (AVFoundation) et le lookup produit côté serveur. Si
un format de string n'est pas reconnu, le décodage retourne ``None`` et
l'app affiche "Produit inconnu" même pour un vrai produit.

3 parsers, chacun pour un format d'entrée distinct :
  - ``parse_gs1_string`` : format avec parenthèses ``(01)037...``
    → produit par treepoem ou copié-collé depuis le PDF
  - ``parse_gs1_digits`` : digits bruts sans séparateur ni parenthèses
  - ``parse_gs1_raw`` : format iOS AVFoundation (préfixe AIM ``]C1``
    optionnel + FNC1 ``\\x1d`` comme séparateur d'AI variables)

Et le **point d'entrée unifié** ``parse_gs1_to_entry`` qui essaie les 3
dans l'ordre approprié et retourne ``{ean, lot, ddm}`` ou ``None``.

Pas de mocking — fonctions pures.
"""
from __future__ import annotations

from common.services.etiquette_palette_service import (
    parse_gs1_digits,
    parse_gs1_raw,
    parse_gs1_string,
    parse_gs1_to_entry,
)

# ─── parse_gs1_string (format avec parenthèses) ────────────────────────────

class TestParseGs1String:
    def test_full_payload_with_parens(self):
        ais = parse_gs1_string("(01)03770014427250(15)270511(10)110527")
        assert ais == {"01": "03770014427250", "15": "270511", "10": "110527"}

    def test_lot_can_be_alphanumeric(self):
        ais = parse_gs1_string("(01)03770014427250(15)270511(10)TESTLOT01")
        assert ais["10"] == "TESTLOT01"

    def test_empty_string_returns_empty_dict(self):
        assert parse_gs1_string("") == {}
        assert parse_gs1_string(None) == {}  # type: ignore[arg-type]

    def test_fallback_extracts_ai_01_from_pure_digits(self):
        # Pas de parens mais commence par "01" + 14 digits → fallback
        ais = parse_gs1_string("01037700144272501527051110110527")
        # Le fallback extrait seulement AI 01 (longueur fixe connue)
        assert ais == {"01": "03770014427250"}


# ─── parse_gs1_digits (digits bruts) ───────────────────────────────────────

class TestParseGs1Digits:
    def test_full_payload_with_numeric_lot(self):
        ais = parse_gs1_digits("01037700144272501527051110110527")
        assert ais == {"01": "03770014427250", "15": "270511", "10": "110527"}

    def test_strips_non_digit_chars(self):
        # Espaces ou tirets : ignorés silencieusement
        ais = parse_gs1_digits("01-03770014427250-15-270511-10-110527")
        assert ais["01"] == "03770014427250"
        assert ais["15"] == "270511"
        assert ais["10"] == "110527"

    def test_empty_returns_empty_dict(self):
        assert parse_gs1_digits("") == {}
        assert parse_gs1_digits(None) == {}  # type: ignore[arg-type]


# ─── parse_gs1_raw (format iOS AVFoundation) ───────────────────────────────

class TestParseGs1Raw:
    def test_with_aim_prefix_and_fnc1(self):
        # Format typique iPhone AVFoundation pour un Code 128 GS1
        # ]C1 = AIM identifier, \x1d = FNC1 séparateur après AI variable
        raw = "]C1010377001442725015270511\x1d10TESTLOT01"
        ais = parse_gs1_raw(raw)
        assert ais == {"01": "03770014427250", "15": "270511", "10": "TESTLOT01"}

    def test_without_aim_prefix_with_fnc1(self):
        raw = "010377001442725015270511\x1d10TESTLOT01"
        ais = parse_gs1_raw(raw)
        assert ais == {"01": "03770014427250", "15": "270511", "10": "TESTLOT01"}

    def test_without_fnc1_compact(self):
        # iOS strip parfois le FNC1 — on doit quand même retrouver l'AI 10
        # car perLayer est unambigu (AI 10 après 15 + 6 digits)
        raw = "01037700144272501527051110TESTLOT01"
        ais = parse_gs1_raw(raw)
        assert ais["01"] == "03770014427250"
        assert ais["15"] == "270511"
        assert ais["10"] == "TESTLOT01"

    def test_returns_empty_dict_if_unknown_ai(self):
        # Commence par "99" → AI inconnu → rien décoder
        ais = parse_gs1_raw("9912345")
        assert ais == {}

    def test_empty_returns_empty_dict(self):
        assert parse_gs1_raw("") == {}
        assert parse_gs1_raw(None) == {}  # type: ignore[arg-type]


# ─── parse_gs1_to_entry (point d'entrée unifié) ────────────────────────────

class TestParseGs1ToEntry:
    """Le format de retour : ``{ean, lot, ddm}`` où ddm est "YYYY-MM-DD"
    ou string vide si non parsable. Retourne ``None`` si pas d'AI 01."""

    def test_format_with_parens(self):
        result = parse_gs1_to_entry("(01)03770014427250(15)270511(10)TESTLOT01")
        assert result == {
            "ean": "03770014427250",
            "lot": "TESTLOT01",
            "ddm": "2027-05-11",
        }

    def test_format_ios_with_aim_and_fnc1(self):
        result = parse_gs1_to_entry(
            "]C1010377001442725015270511\x1d10TESTLOT01"
        )
        assert result == {
            "ean": "03770014427250",
            "lot": "TESTLOT01",
            "ddm": "2027-05-11",
        }

    def test_format_ios_without_aim(self):
        result = parse_gs1_to_entry("010377001442725015270511\x1d10TESTLOT01")
        assert result == {
            "ean": "03770014427250",
            "lot": "TESTLOT01",
            "ddm": "2027-05-11",
        }

    def test_format_pure_digits_numeric_lot(self):
        result = parse_gs1_to_entry("01037700144272501527051110110527")
        assert result == {
            "ean": "03770014427250",
            "lot": "110527",
            "ddm": "2027-05-11",
        }

    def test_format_compact_no_fnc1_alphanumeric_lot(self):
        result = parse_gs1_to_entry("01037700144272501527051110TESTLOT01")
        assert result is not None
        assert result["ean"] == "03770014427250"
        assert result["ddm"] == "2027-05-11"
        # Le lot doit être correctement extrait
        assert result["lot"] == "TESTLOT01"

    def test_returns_none_if_no_ean(self):
        assert parse_gs1_to_entry("") is None
        assert parse_gs1_to_entry("garbage") is None
        # AI inconnu seul
        assert parse_gs1_to_entry("(99)123") is None

    def test_ai_17_used_as_ddm_fallback_if_no_15(self):
        # AI 17 = "use by date" — accepté comme fallback de AI 15 (DDM)
        result = parse_gs1_to_entry("(01)03770014427250(17)260101(10)BATCH")
        assert result is not None
        assert result["ddm"] == "2026-01-01"

    def test_ddm_empty_if_invalid(self):
        # DDM 99/99/99 = mois invalide → parser doit retourner string vide
        result = parse_gs1_to_entry("(01)03770014427250(15)999999(10)BATCH")
        assert result is not None
        assert result["ddm"] == ""

    def test_lot_empty_if_absent(self):
        result = parse_gs1_to_entry("(01)03770014427250(15)270511")
        assert result is not None
        assert result["lot"] == ""

    def test_whitespace_around_input_tolerated(self):
        result = parse_gs1_to_entry("   (01)03770014427250(15)270511   ")
        assert result is not None
        assert result["ean"] == "03770014427250"
