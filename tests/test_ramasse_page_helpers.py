"""Tests for pure helpers in common/ramasse_grid — no UI, no DB."""
from __future__ import annotations

import datetime as dt

from common.ramasse_grid import (
    apply_saved_cartons,
    compute_palettes_and_weight,
    format_poids_display,
    insert_gout_separators,
    prepare_grid_rows,
    safe_int,
)


class TestSafeInt:
    def test_int_value(self):
        assert safe_int(42) == 42

    def test_float_value(self):
        assert safe_int(12.7) == 12

    def test_string_int(self):
        assert safe_int("15") == 15

    def test_string_float(self):
        assert safe_int("15.3") == 15

    def test_none_returns_default(self):
        assert safe_int(None) == 0
        assert safe_int(None, default=99) == 99

    def test_empty_string(self):
        assert safe_int("") == 0

    def test_invalid_string(self):
        assert safe_int("abc") == 0

    def test_negative(self):
        assert safe_int(-5) == -5


class TestComputePalettesAndWeight:
    def test_zero_cartons(self):
        nb_pal, poids = compute_palettes_and_weight(0, 7.5, 60)
        assert nb_pal == 0
        assert poids == 0

    def test_basic_case(self):
        # 60 cartons × 7.5 kg = 450 kg + 1 palette × 25 kg = 475 kg
        nb_pal, poids = compute_palettes_and_weight(60, 7.5, 60)
        assert nb_pal == 1
        assert poids == 475

    def test_ceil_palettes(self):
        # 61 cartons, capacity 60 → 2 palettes
        nb_pal, poids = compute_palettes_and_weight(61, 7.5, 60)
        assert nb_pal == 2
        # 61 × 7.5 + 2 × 25 = 457.5 + 50 = 507.5 → 508
        assert poids == 508

    def test_capacity_zero_returns_zero_palettes(self):
        nb_pal, poids = compute_palettes_and_weight(100, 7.5, 0)
        assert nb_pal == 0
        # poids = 100 × 7.5 + 0 × 25 = 750
        assert poids == 750


class TestFormatPoidsDisplay:
    def test_zero_returns_dash(self):
        assert format_poids_display(0) == "—"

    def test_small_value(self):
        assert format_poids_display(250) == "250 kg"

    def test_thousand_separator(self):
        assert format_poids_display(12500) == "12 500 kg"


class TestPrepareGridRows:
    def test_empty(self):
        assert prepare_grid_rows([], {}) == []

    def test_single_row(self):
        rows = [{
            "Référence": "KEF-ORG-12x33",
            "Produit (goût + format)": "Kéfir Original — 12x33cl",
            "DDM": dt.date(2027, 3, 15),
        }]
        meta = {"Kéfir Original — 12x33cl": {"_poids_carton": 7.5, "_palette_capacity": 60}}
        result = prepare_grid_rows(rows, meta)
        assert len(result) == 1
        r = result[0]
        assert r["ref"] == "KEF-ORG-12x33"
        assert r["produit"] == "Kéfir Original — 12x33cl"
        assert r["_gout"] == "Kéfir Original"
        assert r["ddm"] == "15/03/2027"
        assert r["cartons"] is None
        assert r["poids_u"] == 7.5
        assert r["pal_cap"] == 60
        assert r["palettes"] == 0
        assert r["poids"] == 0
        assert r["poids_display"] == "—"

    def test_label_without_separator(self):
        """Si le label n'a pas de ' — ', _gout = label complet."""
        rows = [{"Référence": "X", "Produit (goût + format)": "Produit simple", "DDM": "01/01/2027"}]
        result = prepare_grid_rows(rows, {})
        assert result[0]["_gout"] == "Produit simple"

    def test_missing_meta(self):
        """Un produit sans meta par label → poids_u=0, pal_cap=0."""
        rows = [{"Référence": "X", "Produit (goût + format)": "A — 12x33", "DDM": "01/01/2027"}]
        result = prepare_grid_rows(rows, {})
        assert result[0]["poids_u"] == 0.0
        assert result[0]["pal_cap"] == 0


class TestApplySavedCartons:
    def test_no_saved(self):
        rows = [{"ref": "A", "cartons": None, "palettes": 0, "poids": 0,
                 "poids_display": "—", "poids_u": 7.5, "pal_cap": 60}]
        apply_saved_cartons(rows, {})
        assert rows[0]["cartons"] is None

    def test_apply_single(self):
        rows = [{"ref": "A", "cartons": None, "palettes": 0, "poids": 0,
                 "poids_display": "—", "poids_u": 7.5, "pal_cap": 60}]
        apply_saved_cartons(rows, {"A": 30})
        assert rows[0]["cartons"] == 30
        assert rows[0]["palettes"] == 1  # ceil(30/60) = 1
        # 30 × 7.5 + 1 × 25 = 225 + 25 = 250
        assert rows[0]["poids"] == 250
        assert rows[0]["poids_display"] == "250 kg"

    def test_preserve_unrestored(self):
        rows = [
            {"ref": "A", "cartons": None, "palettes": 0, "poids": 0,
             "poids_display": "—", "poids_u": 7.5, "pal_cap": 60},
            {"ref": "B", "cartons": None, "palettes": 0, "poids": 0,
             "poids_display": "—", "poids_u": 7.5, "pal_cap": 60},
        ]
        apply_saved_cartons(rows, {"A": 30})
        assert rows[0]["cartons"] == 30
        # B is untouched
        assert rows[1]["cartons"] is None
        assert rows[1]["palettes"] == 0


class TestInsertGoutSeparators:
    def test_empty(self):
        assert insert_gout_separators([]) == []

    def test_single_gout(self):
        rows = [
            {"_gout": "Kéfir Original", "ref": "A"},
            {"_gout": "Kéfir Original", "ref": "B"},
        ]
        result = insert_gout_separators(rows)
        assert len(result) == 3  # 1 sep + 2 rows
        assert result[0]["_sep"] is True
        assert result[0]["_gout"] == "Kéfir Original"
        assert result[1]["ref"] == "A"
        assert result[2]["ref"] == "B"

    def test_multiple_gouts(self):
        rows = [
            {"_gout": "Kéfir Original", "ref": "A"},
            {"_gout": "Kéfir Original", "ref": "B"},
            {"_gout": "Kéfir Gingembre", "ref": "C"},
        ]
        result = insert_gout_separators(rows)
        assert len(result) == 5  # 2 seps + 3 rows
        assert result[0]["_sep"] is True
        assert result[0]["_gout"] == "Kéfir Original"
        assert result[3]["_sep"] is True
        assert result[3]["_gout"] == "Kéfir Gingembre"

    def test_does_not_mutate_input(self):
        rows = [{"_gout": "A", "ref": "X"}]
        insert_gout_separators(rows)
        assert len(rows) == 1  # unchanged
