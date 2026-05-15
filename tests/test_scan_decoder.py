"""Tests pour scan_decoder — détection du type d'un scan douchette.

Couvre les formats qu'on peut recevoir d'une douchette HID :
- SSCC palette nus (18 digits)
- GS1-128 HRI (parenthèses, lisible humain)
- GS1-128 raw (digits collés, FNC1 invisible ou présent)
- EAN-13 (carton supermarché)
- GTIN-14 (carton logistique)
- URL (QR code site web)
- Texte libre
"""
from __future__ import annotations

from common.services.scan_decoder import decode_scan


class TestEmpty:

    def test_empty_string(self):
        r = decode_scan("")
        assert r.type == "empty"
        assert r.raw == ""

    def test_none_treated_as_empty(self):
        r = decode_scan(None)  # type: ignore[arg-type]
        assert r.type == "empty"


class TestSSCC:
    """Le format clé pour /chargement-camion : SSCC 18 digits."""

    def test_sscc_18_digits(self):
        r = decode_scan("337700144200000005")
        assert r.type == "sscc"
        assert r.sscc == "337700144200000005"
        assert "SSCC" in r.summary
        # Mise en forme groupée pour lisibilité humaine
        assert "3377 0014 4200 0000 05" in r.summary


class TestEAN13:

    def test_ean13_supermarket(self):
        r = decode_scan("3770014427250")
        assert r.type == "ean13"
        assert "EAN-13" in r.summary


class TestGTIN14:

    def test_gtin14_logistics(self):
        r = decode_scan("03770014427250")
        assert r.type == "gtin14"
        assert "GTIN-14" in r.summary


class TestGS1HRI:
    """GS1-128 au format human-readable (avec parenthèses)."""

    def test_sscc_with_parens(self):
        r = decode_scan("(00)337700144200000005")
        assert r.type == "gs1_128_hri"
        assert r.sscc == "337700144200000005"
        assert r.ais.get("00") == "337700144200000005"

    def test_full_palette_label(self):
        """Format typique d'une étiquette palette Ferment Station."""
        raw = "(00)337700144200000005(02)23770014427049(15)270508(10)L080527(37)126"
        r = decode_scan(raw)
        assert r.type == "gs1_128_hri"
        assert r.sscc == "337700144200000005"
        assert r.ais.get("02") == "23770014427049"
        assert r.ais.get("15") == "270508"
        assert r.ais.get("10") == "L080527"
        assert r.ais.get("37") == "126"
        # Le summary doit mentionner les éléments clés
        assert "SSCC" in r.summary
        assert "GTIN" in r.summary
        assert "Lot L080527" in r.summary
        assert "DDM 270508" in r.summary

    def test_gtin_only_with_parens(self):
        r = decode_scan("(01)03770014427250")
        assert r.type == "gs1_128_hri"
        assert r.ais.get("01") == "03770014427250"


class TestGS1Raw:
    """GS1-128 brut (digits collés, sans parenthèses)."""

    def test_sscc_raw_starts_with_00(self):
        # SSCC en digits raw : AI 00 + 18 digits = 20 digits commençant par 00
        r = decode_scan("00337700144200000005")
        assert r.type == "gs1_128_raw"
        assert r.sscc == "337700144200000005"

    def test_with_fnc1_separator(self):
        # FNC1 ASCII GS = \x1d, séparateur des AIs variables
        raw = "00337700144200000005\x1d02237700144270491527050810L080527"
        r = decode_scan(raw)
        assert r.type == "gs1_128_raw"
        # Le FNC1 doit être rendu visible dans normalized
        assert "|" in r.normalized
        # SSCC bien extrait malgré le FNC1
        assert r.sscc == "337700144200000005"


class TestURL:

    def test_qr_code_url(self):
        r = decode_scan("https://prod.symbiose-kefir.fr/chargement-camion")
        assert r.type == "url"
        assert "URL" in r.summary

    def test_http_url(self):
        r = decode_scan("http://example.com")
        assert r.type == "url"


class TestText:

    def test_random_text(self):
        r = decode_scan("HELLO WORLD")
        assert r.type == "text"

    def test_short_digits_not_matching_known_formats(self):
        # 5 digits → pas un format connu
        r = decode_scan("12345")
        assert r.type == "text"
        assert "5 chiffres" in r.summary
