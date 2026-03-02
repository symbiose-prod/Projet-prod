"""Tests for common/ramasse.py — format detection, label parsing, barcode matrix."""
from __future__ import annotations

import pytest

from common.ramasse import (
    format_from_stock,
    extract_gout,
    clean_product_label,
    parse_barcode_matrix,
    _canon,
)


# ─── format_from_stock ────────────────────────────────────────────────────────

class TestFormatFromStock:

    def test_12x33(self):
        assert format_from_stock("Carton de 12 Bouteilles - 0,33L") == "12x33"

    def test_6x75(self):
        assert format_from_stock("Carton de 6 - 75cl") == "6x75"

    def test_4x75(self):
        assert format_from_stock("Pack de 4 - 75cl") == "4x75"

    def test_12x33cl_compact(self):
        # NOTE: compact "12x33cl" not matched — \b fails when 'x' follows digit.
        # format_from_stock expects "Carton de 12 … 33cl" phrasing.
        assert format_from_stock("12x33cl") is None

    def test_none_returns_none(self):
        assert format_from_stock(None) is None

    def test_empty_returns_none(self):
        assert format_from_stock("") is None

    def test_unknown_format(self):
        assert format_from_stock("Something random") is None

    def test_unicode_multiply(self):
        # Same limitation as compact format — \b word boundary issue
        assert format_from_stock("12×33cl") is None


# ─── extract_gout ─────────────────────────────────────────────────────────────

class TestExtractGout:

    def test_kefir_gingembre(self):
        assert extract_gout("Kéfir Gingembre") == "Gingembre"

    def test_kefir_de_fruits_original(self):
        assert extract_gout("Kéfir de fruits Original") == "Original"

    def test_infusion_menthe_poivree(self):
        result = extract_gout("Infusion de Kéfir de fruits Menthe Poivrée")
        assert result == "Menthe Poivrée"

    def test_infusion_probiotique(self):
        result = extract_gout("Infusion probiotique Mélisse")
        assert result == "Mélisse"

    def test_plain_label(self):
        assert extract_gout("Something Else") == "Something Else"


# ─── clean_product_label ──────────────────────────────────────────────────────

class TestCleanProductLabel:

    def test_remove_degree_suffix(self):
        assert clean_product_label("Kefir Peche - 0.0°") == "Kefir Peche"

    def test_no_suffix(self):
        assert clean_product_label("Kefir Original") == "Kefir Original"

    def test_none_input(self):
        assert clean_product_label(None) == ""


# ─── parse_barcode_matrix ─────────────────────────────────────────────────────

class TestParseBarcodeMatrix:

    def _make_matrix(self, entries: list[dict]) -> dict:
        return {"produits": [{"codesBarres": entries}]}

    def test_valid_entry(self):
        entry = {
            "code": "3770014427014",
            "modeleProduit": {"idProduit": 42},
            "modeleContenant": {"contenance": 0.33},
            "modeleLot": {"libelle": "Carton de 12"},
        }
        result = parse_barcode_matrix(self._make_matrix([entry]))
        assert 42 in result
        assert len(result[42]) == 1
        assert result[42][0]["ref6"] == "427014"
        assert result[42][0]["fmt_str"] == "12x33"

    def test_6x75(self):
        entry = {
            "code": "3770014999999",
            "modeleProduit": {"idProduit": 7},
            "modeleContenant": {"contenance": 0.75},
            "modeleLot": {"libelle": "Pack de 6"},
        }
        result = parse_barcode_matrix(self._make_matrix([entry]))
        assert result[7][0]["fmt_str"] == "6x75"

    def test_missing_fields_skipped(self):
        entry = {
            "code": "",
            "modeleProduit": {"idProduit": 42},
            "modeleContenant": {"contenance": 0.33},
            "modeleLot": {"libelle": "Carton de 12"},
        }
        result = parse_barcode_matrix(self._make_matrix([entry]))
        assert 42 not in result

    def test_empty_matrix(self):
        result = parse_barcode_matrix({"produits": []})
        assert result == {}


# ─── _canon ───────────────────────────────────────────────────────────────────

class TestCanon:

    def test_accent_removal(self):
        assert _canon("Pêche") == "peche"

    def test_special_chars(self):
        assert _canon("Kéfir-Original!") == "kefir original"
