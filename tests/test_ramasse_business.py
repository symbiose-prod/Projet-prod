"""Tests for common/ramasse.py — business logic: weights, palettes, destinataires, DDM."""
from __future__ import annotations

from common.ramasse import (
    CARTON_WEIGHTS_FALLBACK,
    PALETTE_CAPACITY,
    build_packaging_summary,
    get_carton_weight,
    get_palette_capacity,
    load_destinataires,
    load_packaging_items,
    today_paris,
)

# ─── get_carton_weight ──────────────────────────────────────────────────────


class TestGetCartonWeight:

    def test_12x33_fallback(self):
        result = get_carton_weight("12x33", "Kéfir Original")
        assert result == CARTON_WEIGHTS_FALLBACK["12x33"]

    def test_6x75_default(self):
        result = get_carton_weight("6x75", "Kéfir Gingembre")
        assert result == CARTON_WEIGHTS_FALLBACK["6x75"]

    def test_6x75_niko_override(self):
        """Niko keyword triggers SAFT weight override."""
        result = get_carton_weight("6x75", "Kéfir Niko Edition")
        assert result == 6.84

    def test_4x75_fallback(self):
        result = get_carton_weight("4x75", "Kéfir Original")
        assert result == CARTON_WEIGHTS_FALLBACK["4x75"]

    def test_unknown_format_returns_zero(self):
        result = get_carton_weight("99x99", "Something")
        assert result == 0.0

    def test_eb_weights_priority(self):
        """EasyBeer dynamic weights take priority over fallback."""
        eb = {(42, "12x33"): 7.5}
        result = get_carton_weight("12x33", "Kéfir", id_produit=42, eb_weights=eb)
        assert result == 7.5

    def test_eb_weights_fallback_when_missing(self):
        """Falls back to static when EB weights don't have the product."""
        eb = {(99, "12x33"): 7.5}
        result = get_carton_weight("12x33", "Kéfir", id_produit=42, eb_weights=eb)
        assert result == CARTON_WEIGHTS_FALLBACK["12x33"]

    def test_eb_weights_zero_falls_back(self):
        """EB weight of 0 is treated as missing → fallback."""
        eb = {(42, "12x33"): 0.0}
        result = get_carton_weight("12x33", "Kéfir", id_produit=42, eb_weights=eb)
        assert result == CARTON_WEIGHTS_FALLBACK["12x33"]

    def test_format_normalization(self):
        """Format with 'cl' suffix and spaces is normalized."""
        result = get_carton_weight("12x33cl", "Kéfir Original")
        assert result == CARTON_WEIGHTS_FALLBACK["12x33"]


# ─── get_palette_capacity ───────────────────────────────────────────────────


class TestGetPaletteCapacity:

    def test_12x33(self):
        assert get_palette_capacity("12x33", "Kéfir") == PALETTE_CAPACITY["12x33"]

    def test_6x75_default(self):
        assert get_palette_capacity("6x75", "Kéfir Gingembre") == PALETTE_CAPACITY["6x75"]

    def test_6x75_niko(self):
        assert get_palette_capacity("6x75", "Kéfir Niko") == 84

    def test_4x75(self):
        assert get_palette_capacity("4x75", "Kéfir") == PALETTE_CAPACITY["4x75"]

    def test_unknown_format(self):
        assert get_palette_capacity("99x99", "Whatever") == 0


# ─── load_destinataires ─────────────────────────────────────────────────────


class TestLoadDestinataires:

    def test_returns_list(self):
        result = load_destinataires()
        assert isinstance(result, list)

    def test_cached(self):
        """Second call returns same object (cached)."""
        r1 = load_destinataires()
        r2 = load_destinataires()
        assert r1 is r2


# ─── today_paris ────────────────────────────────────────────────────────────


class TestTodayParis:

    def test_returns_date(self):
        import datetime as dt
        result = today_paris()
        assert isinstance(result, dt.date)
        assert not isinstance(result, dt.datetime)


# ─── build_packaging_summary ─────────────────────────────────────────────────


class TestBuildPackagingSummary:

    def test_filters_zero_qty(self):
        items = [
            {"label": "Palettes bois", "qty": 3, "unit": "palette"},
            {"label": "Film", "qty": 0, "unit": "rouleau"},
        ]
        result = build_packaging_summary(items)
        assert len(result) == 1
        assert result[0]["label"] == "Palettes bois"
        assert result[0]["qty"] == 3

    def test_empty_input(self):
        assert build_packaging_summary([]) == []

    def test_all_zero(self):
        items = [{"label": "A", "qty": 0}, {"label": "B", "qty": 0}]
        assert build_packaging_summary(items) == []

    def test_missing_qty_treated_as_zero(self):
        items = [{"label": "A"}, {"label": "B", "qty": 5, "unit": "u"}]
        result = build_packaging_summary(items)
        assert len(result) == 1
        assert result[0]["label"] == "B"

    def test_default_unit_is_palette(self):
        items = [{"label": "Test", "qty": 2}]
        result = build_packaging_summary(items)
        assert result[0]["unit"] == "palette"


# ─── load_packaging_items ─────────────────────────────────────────────────────


class TestLoadPackagingItems:

    def test_unknown_recipient_returns_empty(self):
        assert load_packaging_items("Inexistant Corp") == []

    def test_returns_list(self):
        # Should always return a list, even if recipient has no items
        result = load_packaging_items("Sofripa")
        assert isinstance(result, list)
