"""
Tests for core/optimizer/planning.py
=====================================
Covers: _weekly_perte, _equalize_last_batch_global, compute_plan.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.optimizer.planning import (
    _equalize_last_batch_global,
    _weekly_perte,
    compute_plan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(
    produit: str = "Kefir Original 33cl",
    gout: str = "Original",
    stock: str = "Carton de 12 Bouteilles - 0,33L",
    qty_vendue: float = 100,
    vol_vendu: float = 3.96,
    qty_dispo: float = 50,
    vol_dispo: float = 1.98,
) -> dict:
    """Build a single valid row for compute_plan input."""
    return {
        "Produit": produit,
        "GoutCanon": gout,
        "Stock": stock,
        "Quantite vendue": qty_vendue,
        "Volume vendu (hl)": vol_vendu,
        "Quantite disponible": qty_dispo,
        "Volume disponible (hl)": vol_dispo,
    }


def _make_df(rows: list[dict] | None = None) -> pd.DataFrame:
    """
    Build a minimal valid DataFrame for compute_plan.

    Uses the French column names with accented characters exactly as
    expected by the production code.
    """
    if rows is None:
        rows = [_make_row()]
    df = pd.DataFrame(rows)
    # Rename to the exact accented column names expected by compute_plan
    df = df.rename(columns={"Quantite vendue": "Quantite\u0301 vendue"})
    return df


# ---------------------------------------------------------------------------
# TestWeeklyPerte
# ---------------------------------------------------------------------------

class TestWeeklyPerte:
    """Tests for _weekly_perte(stock_hl, vitesse_hl_j, price_hL)."""

    def test_zero_stock_positive_speed(self):
        """No stock, 1 hL/day demand -> full loss over 7 days."""
        result = _weekly_perte(0.0, 1.0, 400.0)
        np.testing.assert_allclose(result, 7.0 * 1.0 * 400.0)

    def test_enough_stock_no_loss(self):
        """Stock covers 7 days of demand -> zero loss."""
        result = _weekly_perte(10.0, 1.0, 400.0)
        np.testing.assert_allclose(result, 0.0)

    def test_partial_stock(self):
        """Stock covers part of the 7-day demand -> partial loss."""
        # demand = 7*2 = 14, stock = 5, manque = 9
        result = _weekly_perte(5.0, 2.0, 400.0)
        np.testing.assert_allclose(result, 9.0 * 400.0)

    def test_zero_speed_no_demand(self):
        """Zero consumption speed -> no demand, no loss."""
        result = _weekly_perte(0.0, 0.0, 400.0)
        np.testing.assert_allclose(result, 0.0)

    def test_negative_stock_treated_as_zero(self):
        """Negative stock is clamped to 0 -> full loss."""
        result = _weekly_perte(-5.0, 1.0, 400.0)
        np.testing.assert_allclose(result, 7.0 * 1.0 * 400.0)

    def test_negative_speed_treated_as_zero(self):
        """Negative speed is clamped to 0 -> no demand, no loss."""
        result = _weekly_perte(10.0, -3.0, 400.0)
        np.testing.assert_allclose(result, 0.0)

    def test_custom_price(self):
        """Custom price is applied correctly."""
        result = _weekly_perte(0.0, 1.0, 1000.0)
        np.testing.assert_allclose(result, 7.0 * 1000.0)

    def test_default_price_is_400(self):
        """Default price parameter is 400 EUR/hL."""
        result_explicit = _weekly_perte(0.0, 1.0, 400.0)
        result_default = _weekly_perte(0.0, 1.0)
        np.testing.assert_allclose(result_default, result_explicit)


# ---------------------------------------------------------------------------
# TestEqualizeLastBatchGlobal
# ---------------------------------------------------------------------------

class TestEqualizeLastBatchGlobal:
    """Tests for _equalize_last_batch_global(Gi, vi, V)."""

    def test_single_item_no_stock(self):
        """One item with zero stock receives the full volume."""
        Gi = np.array([0.0])
        vi = np.array([1.0])
        x = _equalize_last_batch_global(Gi, vi, 10.0)
        np.testing.assert_allclose(x.sum(), 10.0, atol=1e-6)
        np.testing.assert_allclose(x[0], 10.0, atol=1e-6)

    def test_two_equal_items_no_stock(self):
        """Two identical items with no stock split volume evenly."""
        Gi = np.array([0.0, 0.0])
        vi = np.array([1.0, 1.0])
        x = _equalize_last_batch_global(Gi, vi, 10.0)
        np.testing.assert_allclose(x.sum(), 10.0, atol=1e-6)
        np.testing.assert_allclose(x[0], x[1], atol=1e-6)

    def test_two_items_asymmetric_stock(self):
        """Item with less stock gets more volume to equalize horizon."""
        Gi = np.array([10.0, 0.0])
        vi = np.array([1.0, 1.0])
        x = _equalize_last_batch_global(Gi, vi, 10.0)
        np.testing.assert_allclose(x.sum(), 10.0, atol=1e-6)
        # Item 1 (Gi=0) should receive more than item 0 (Gi=10)
        assert x[1] > x[0], f"Expected x[1]>x[0], got {x[1]} vs {x[0]}"

    def test_zero_volume_returns_zeros(self):
        """If V=0, no allocation is made."""
        Gi = np.array([5.0, 3.0])
        vi = np.array([1.0, 2.0])
        x = _equalize_last_batch_global(Gi, vi, 0.0)
        np.testing.assert_allclose(x, np.zeros(2), atol=1e-12)

    def test_zero_speeds_returns_zeros(self):
        """If all speeds are zero, no allocation is made."""
        Gi = np.array([5.0, 3.0])
        vi = np.array([0.0, 0.0])
        x = _equalize_last_batch_global(Gi, vi, 10.0)
        np.testing.assert_allclose(x, np.zeros(2), atol=1e-12)

    def test_high_stock_small_volume(self):
        """When stock is already high, small volume is still distributed."""
        Gi = np.array([100.0, 100.0])
        vi = np.array([1.0, 1.0])
        V = 2.0
        x = _equalize_last_batch_global(Gi, vi, V)
        np.testing.assert_allclose(x.sum(), V, atol=1e-6)

    def test_single_item_with_stock(self):
        """Single item with existing stock still receives full volume."""
        Gi = np.array([5.0])
        vi = np.array([1.0])
        x = _equalize_last_batch_global(Gi, vi, 10.0)
        np.testing.assert_allclose(x.sum(), 10.0, atol=1e-6)

    def test_total_allocated_equals_V(self):
        """Sum of allocated volumes matches V (conservation)."""
        Gi = np.array([3.0, 7.0, 1.0])
        vi = np.array([2.0, 1.0, 3.0])
        V = 20.0
        x = _equalize_last_batch_global(Gi, vi, V)
        np.testing.assert_allclose(x.sum(), V, atol=1e-6)

    def test_negative_Gi_treated_as_zero(self):
        """Negative stock values are clamped to 0."""
        Gi = np.array([-5.0, 0.0])
        vi = np.array([1.0, 1.0])
        x = _equalize_last_batch_global(Gi, vi, 10.0)
        np.testing.assert_allclose(x.sum(), 10.0, atol=1e-6)
        # Both items effectively start at 0 stock, so equal split
        np.testing.assert_allclose(x[0], x[1], atol=1e-6)

    def test_large_volume_converges(self):
        """Even very large V produces a valid, convergent allocation."""
        Gi = np.array([1.0, 2.0])
        vi = np.array([0.5, 1.5])
        V = 100000.0
        x = _equalize_last_batch_global(Gi, vi, V)
        np.testing.assert_allclose(x.sum(), V, atol=1e-3)
        # Both x values should be non-negative
        assert np.all(x >= -1e-12)

    def test_equalization_horizon(self):
        """Verify that horizons (Gi + xi) / vi are approximately equal."""
        Gi = np.array([2.0, 8.0])
        vi = np.array([1.0, 1.0])
        V = 10.0
        x = _equalize_last_batch_global(Gi, vi, V)
        horizons = (Gi + x) / vi
        # The horizons should be close to each other (that's what equalization does)
        np.testing.assert_allclose(horizons[0], horizons[1], atol=1e-4)


# ---------------------------------------------------------------------------
# TestComputePlan
# ---------------------------------------------------------------------------

# Exact accented column names expected by compute_plan
_COLS = [
    "Produit", "GoutCanon", "Stock", "Quantit\u00e9 vendue",
    "Volume vendu (hl)", "Quantit\u00e9 disponible", "Volume disponible (hl)",
]


def _build_df(rows: list[dict]) -> pd.DataFrame:
    """
    Build a DataFrame with the exact columns expected by compute_plan.
    Each row dict should provide values for the columns in _COLS.
    """
    return pd.DataFrame(rows, columns=_COLS)


def _default_row(
    produit: str = "Kefir Original 33cl",
    gout: str = "Original",
    stock: str = "Carton de 12 Bouteilles - 0,33L",
    qty_vendue: float = 100,
    vol_vendu: float = 3.96,
    qty_dispo: float = 50,
    vol_dispo: float = 1.98,
) -> dict:
    """Return a single valid row as dict matching _COLS."""
    return dict(zip(_COLS, [
        produit, gout, stock, qty_vendue, vol_vendu, qty_dispo, vol_dispo,
    ]))


class TestComputePlan:
    """Tests for compute_plan."""

    # ----- Column validation -----

    def test_missing_columns_raises(self):
        """Missing required columns raise ValueError with column names."""
        df = pd.DataFrame({"Produit": ["A"], "GoutCanon": ["B"]})
        with pytest.raises(ValueError, match="Colonnes manquantes"):
            compute_plan(df, 60, 50.0, 2, False, None, None)

    def test_missing_single_column_reports_it(self):
        """Error message lists the specific missing column."""
        df = _build_df([_default_row()])
        df = df.drop(columns=["Volume vendu (hl)"])
        with pytest.raises(ValueError, match="Volume vendu"):
            compute_plan(df, 60, 50.0, 2, False, None, None)

    # ----- Nominal cases -----

    def test_nominal_two_flavors_returns_7_tuple(self):
        """Nominal case with 2 flavors returns a 7-element tuple."""
        rows = [
            _default_row(produit="Kefir Original 33cl", gout="Original",
                         stock="Carton de 12 Bouteilles - 0,33L",
                         vol_vendu=4.0, vol_dispo=2.0),
            _default_row(produit="Kefir Gingembre 33cl", gout="Gingembre",
                         stock="Carton de 12 Bouteilles - 0,33L",
                         vol_vendu=3.0, vol_dispo=1.0),
        ]
        df = _build_df(rows)
        result = compute_plan(df, 60, 50.0, 2, False, None, None)
        assert len(result) == 7
        df_min, cap_resume, sel_gouts, synth_sel, df_calc, df_out, note_msg = result
        assert isinstance(df_min, pd.DataFrame)
        assert isinstance(cap_resume, str)
        assert isinstance(sel_gouts, list)
        assert isinstance(synth_sel, pd.DataFrame)
        assert isinstance(note_msg, str)

    def test_single_flavor_works(self):
        """A single flavor with nb_gouts=1 produces a valid plan."""
        rows = [
            _default_row(produit="Kefir Original 33cl", gout="Original",
                         stock="Carton de 12 Bouteilles - 0,33L",
                         vol_vendu=5.0, vol_dispo=2.0),
        ]
        df = _build_df(rows)
        result = compute_plan(df, 60, 30.0, 1, False, None, None)
        assert len(result) == 7
        df_min = result[0]
        sel_gouts = result[2]
        assert sel_gouts == ["Original"]
        assert len(df_min) >= 1

    # ----- Filtering -----

    def test_exclude_list_filters_flavor(self):
        """exclude_list removes the specified flavor from the plan."""
        rows = [
            _default_row(gout="Original", vol_vendu=5.0, vol_dispo=1.0),
            _default_row(produit="Kefir Gingembre 33cl", gout="Gingembre",
                         stock="Carton de 12 Bouteilles - 0,33L",
                         vol_vendu=3.0, vol_dispo=1.0),
        ]
        df = _build_df(rows)
        result = compute_plan(df, 60, 30.0, 2, False, None, ["Original"])
        sel_gouts = result[2]
        assert "Original" not in sel_gouts
        assert "Gingembre" in sel_gouts

    def test_manual_keep_filters_flavor(self):
        """manual_keep retains only the specified flavors."""
        rows = [
            _default_row(gout="Original", vol_vendu=5.0, vol_dispo=1.0),
            _default_row(produit="Kefir Gingembre 33cl", gout="Gingembre",
                         stock="Carton de 12 Bouteilles - 0,33L",
                         vol_vendu=3.0, vol_dispo=1.0),
            _default_row(produit="Kefir Citron 33cl", gout="Citron",
                         stock="Carton de 12 Bouteilles - 0,33L",
                         vol_vendu=2.0, vol_dispo=0.5),
        ]
        df = _build_df(rows)
        result = compute_plan(df, 60, 30.0, 3, False, ["Gingembre", "Citron"], None)
        sel_gouts = result[2]
        assert "Original" not in sel_gouts
        assert "Gingembre" in sel_gouts
        assert "Citron" in sel_gouts

    def test_empty_after_filtering_raises(self):
        """Filtering out all flavors raises ValueError."""
        rows = [_default_row(gout="Original")]
        df = _build_df(rows)
        with pytest.raises(ValueError, match="Aucun go"):
            compute_plan(df, 60, 30.0, 1, False, ["Inexistant"], None)

    # ----- Volume allocation -----

    def test_volume_zero_returns_zero_production(self):
        """Volume cible of 0 -> no production allocated."""
        rows = [
            _default_row(gout="Original", vol_vendu=5.0, vol_dispo=2.0),
        ]
        df = _build_df(rows)
        result = compute_plan(df, 60, 0.0, 1, False, None, None)
        df_calc = result[4]
        total_produced = df_calc["X_adj (hL)"].sum()
        np.testing.assert_allclose(total_produced, 0.0, atol=1e-9)

    def test_total_volume_approximately_matches_target(self):
        """Total allocated volume should approximately match volume_cible."""
        rows = [
            _default_row(gout="Original", vol_vendu=5.0, vol_dispo=2.0),
            _default_row(produit="Kefir Gingembre 33cl", gout="Gingembre",
                         stock="Carton de 12 Bouteilles - 0,33L",
                         vol_vendu=3.0, vol_dispo=1.0),
        ]
        df = _build_df(rows)
        volume_cible = 40.0
        result = compute_plan(df, 60, volume_cible, 2, False, None, None)
        df_calc = result[4]
        total = df_calc["X_adj (hL)"].sum()
        np.testing.assert_allclose(total, volume_cible, atol=1e-3)

    # ----- Output structure -----

    def test_df_min_has_expected_columns(self):
        """df_min output contains the expected display columns."""
        rows = [_default_row(gout="Original", vol_vendu=5.0, vol_dispo=2.0)]
        df = _build_df(rows)
        result = compute_plan(df, 60, 30.0, 1, False, None, None)
        df_min = result[0]
        expected_cols = {
            "GoutCanon", "Produit", "Stock",
            "Cartons \u00e0 produire (arrondi)",
            "Bouteilles \u00e0 produire (arrondi)",
            "Volume produit arrondi (hL)",
        }
        assert expected_cols == set(df_min.columns)

    def test_synth_sel_has_expected_columns(self):
        """synth_sel output has the renamed summary columns."""
        rows = [_default_row(gout="Original", vol_vendu=5.0, vol_dispo=2.0)]
        df = _build_df(rows)
        result = compute_plan(df, 60, 30.0, 1, False, None, None)
        synth_sel = result[3]
        expected = {
            "Ventes 2 mois (hL)", "Stock (hL)", "Vitesse (hL/j)",
            "Autonomie (jours)", "Score urgence",
        }
        assert expected == set(synth_sel.columns)

    # ----- Pro-rata allocation -----

    def test_repartir_pro_rv_distributes_proportionally(self):
        """With repartir_pro_rv=True, volume is split proportional to sales."""
        rows = [
            _default_row(gout="Original", vol_vendu=6.0, vol_dispo=0.0),
            _default_row(produit="Kefir Gingembre 33cl", gout="Gingembre",
                         stock="Carton de 12 Bouteilles - 0,33L",
                         vol_vendu=2.0, vol_dispo=0.0),
        ]
        df = _build_df(rows)
        result = compute_plan(df, 60, 40.0, 2, True, None, None)
        df_calc = result[4]
        vol_original = df_calc.loc[
            df_calc["GoutCanon"] == "Original", "X_adj (hL)"
        ].sum()
        vol_gingembre = df_calc.loc[
            df_calc["GoutCanon"] == "Gingembre", "X_adj (hL)"
        ].sum()
        # Original has 3x the sales -> should get ~3x the volume
        ratio = vol_original / max(vol_gingembre, 1e-12)
        np.testing.assert_allclose(ratio, 3.0, atol=0.5)

    # ----- Multiple formats for same flavor -----

    def test_multiple_formats_same_flavor(self):
        """Multiple bottle formats for the same flavor are aggregated."""
        rows = [
            _default_row(produit="Kefir Original 33cl", gout="Original",
                         stock="Carton de 12 Bouteilles - 0,33L",
                         vol_vendu=3.0, vol_dispo=1.0),
            _default_row(produit="Kefir Original 75cl", gout="Original",
                         stock="Carton de 6 Bouteilles - 0,75L",
                         vol_vendu=2.0, vol_dispo=0.5),
        ]
        df = _build_df(rows)
        result = compute_plan(df, 60, 20.0, 1, False, None, None)
        df_min = result[0]
        # Both formats should appear in output
        assert len(df_min) == 2
        sel_gouts = result[2]
        assert sel_gouts == ["Original"]
