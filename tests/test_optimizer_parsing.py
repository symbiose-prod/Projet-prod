"""Tests for core/optimizer.py — parsing and format functions."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from core.optimizer import (
    parse_stock,
    is_allowed_format,
    detect_header_row,
    parse_days_from_b2,
    safe_num,
    ALLOWED_FORMATS,
)


# ─── parse_stock ──────────────────────────────────────────────────────────────

class TestParseStock:
    """parse_stock(text) → (nb_bottles, vol_L)"""

    def test_carton_12_33cl(self):
        nb, vol = parse_stock("Carton de 12 Bouteilles - 0,33L")
        assert nb == 12 and abs(vol - 0.33) < 0.01

    def test_carton_6_75cl(self):
        nb, vol = parse_stock("Carton de 6 Bouteilles - 0,75L")
        assert nb == 6 and abs(vol - 0.75) < 0.01

    def test_carton_4_75cl(self):
        nb, vol = parse_stock("4x75cl")
        assert nb == 4 and abs(vol - 0.75) < 0.01

    def test_12x33_compact(self):
        nb, vol = parse_stock("12x33cl")
        assert nb == 12 and abs(vol - 0.33) < 0.01

    def test_unicode_multiply(self):
        nb, vol = parse_stock("12×33cl")
        assert nb == 12 and abs(vol - 0.33) < 0.01

    def test_six_bouteilles_75(self):
        nb, vol = parse_stock("6 Bouteilles 75cl")
        assert nb == 6 and abs(vol - 0.75) < 0.01

    def test_nan_returns_nan(self):
        nb, vol = parse_stock(float("nan"))
        assert math.isnan(nb) and math.isnan(vol)

    def test_no_match_returns_partial_nan(self):
        nb, vol = parse_stock("Unknown format")
        assert math.isnan(nb) and math.isnan(vol)

    def test_litre_unit(self):
        nb, vol = parse_stock("Carton de 12 Bouteilles - 0.33L")
        assert nb == 12 and abs(vol - 0.33) < 0.01

    def test_caisse_de_6(self):
        nb, vol = parse_stock("Caisse de 6 - 75cl")
        assert nb == 6 and abs(vol - 0.75) < 0.01


# ─── is_allowed_format ────────────────────────────────────────────────────────

class TestIsAllowedFormat:
    """is_allowed_format(nb, vol, stock_txt) → bool"""

    def test_12x33_allowed(self):
        assert is_allowed_format(12, 0.33, "Carton de 12 - 33cl") is True

    def test_6x75_allowed(self):
        assert is_allowed_format(6, 0.75, "6x75") is True

    def test_4x75_allowed(self):
        assert is_allowed_format(4, 0.75, "4x75cl") is True

    def test_24x33_rejected(self):
        assert is_allowed_format(24, 0.33, "24x33cl") is False

    def test_nan_inputs_rejected(self):
        assert is_allowed_format(float("nan"), float("nan"), "random text") is False

    def test_nan_but_regex_4x75_fallback(self):
        assert is_allowed_format(float("nan"), float("nan"), "4 x 75 cl pack") is True

    def test_vol_tolerance(self):
        # 0.34 is within VOL_TOL=0.02 of 0.33
        assert is_allowed_format(12, 0.34, "") is True

    def test_vol_outside_tolerance(self):
        assert is_allowed_format(12, 0.40, "") is False


# ─── detect_header_row ────────────────────────────────────────────────────────

class TestDetectHeaderRow:

    def test_header_at_row_0(self):
        df = pd.DataFrame({
            "Produit": ["A"],
            "Stock": ["12x33"],
            "Quantité vendue": [10],
            "Volume vendu (hl)": [1.0],
            "Quantité disponible": [5],
            "Volume disponible (hl)": [0.5],
        })
        # When passed as raw (header=None), the header values are at row 0
        raw = pd.read_csv(pd.io.common.StringIO(df.to_csv(index=False)), header=None)
        assert detect_header_row(raw) == 0

    def test_header_at_row_3(self):
        # Build a raw DataFrame where the header is at row 3
        header = ["Produit", "Stock", "Quantité vendue", "Volume vendu (hl)",
                  "Quantité disponible", "Volume disponible (hl)"]
        rows = [["info1"] * 6, ["info2"] * 6, ["info3"] * 6, header, ["data"] * 6]
        raw = pd.DataFrame(rows)
        assert detect_header_row(raw) == 3

    def test_no_header_returns_0(self):
        raw = pd.DataFrame([["a", "b", "c"], ["d", "e", "f"]])
        assert detect_header_row(raw) == 0


# ─── parse_days_from_b2 ──────────────────────────────────────────────────────

class TestParseDaysFromB2:

    def test_integer(self):
        assert parse_days_from_b2(30) == 30

    def test_float(self):
        assert parse_days_from_b2(60.0) == 60

    def test_string_jours(self):
        assert parse_days_from_b2("30 jours") == 30

    def test_string_jour_singular(self):
        assert parse_days_from_b2("1 jour") == 1

    def test_date_range(self):
        days = parse_days_from_b2("01/01/2026 - 31/01/2026")
        assert days == 30

    def test_none_returns_none(self):
        assert parse_days_from_b2(None) is None

    def test_negative_returns_none(self):
        assert parse_days_from_b2(-5) is None

    def test_nan_returns_none(self):
        assert parse_days_from_b2(float("nan")) is None

    def test_plain_number_string(self):
        assert parse_days_from_b2("90") == 90


# ─── safe_num ─────────────────────────────────────────────────────────────────

class TestSafeNum:

    def test_valid_numbers(self):
        s = pd.Series(["1", "2.5", "3"])
        result = safe_num(s)
        assert list(result) == [1.0, 2.5, 3.0]

    def test_coerce_invalid(self):
        s = pd.Series(["abc", "1", None])
        result = safe_num(s)
        assert result.iloc[0] != result.iloc[0]  # NaN
        assert result.iloc[1] == 1.0
