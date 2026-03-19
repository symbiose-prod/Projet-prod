"""Tests for core/optimizer/losses.py — loss/shortage computation."""
from __future__ import annotations

import pandas as pd

from core.optimizer.losses import compute_losses_table_v48


def _make_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestComputeLossesTable:

    def test_none_input_returns_empty(self):
        result = compute_losses_table_v48(None, 30, 400.0)
        assert result.empty
        assert "Goût" in result.columns

    def test_empty_dataframe(self):
        result = compute_losses_table_v48(pd.DataFrame(), 30, 400.0)
        assert result.empty

    def test_missing_gout_canon_column(self):
        df = _make_df([{"col_a": 1}])
        result = compute_losses_table_v48(df, 30, 400.0)
        assert result.empty

    def test_basic_shortage(self):
        """Product selling faster than stock → positive loss."""
        df = _make_df([{
            "GoutCanon": "Gingembre",
            "Quantité vendue": 100,
            "Volume vendu (hl)": 10.0,
            "Quantité disponible": 10,
            "Volume disponible (hl)": 1.0,
        }])
        result = compute_losses_table_v48(df, 30, 400.0)
        assert len(result) == 1
        row = result.iloc[0]
        assert row["Goût"] == "Gingembre"
        # Demand 7j = 10/30 * 7 = 2.33 hL, stock = 1.0 hL → shortage = 1.33
        assert row["Manque sur 7 j (hL)"] > 0
        assert row["Perte (€)"] > 0

    def test_no_shortage(self):
        """Stock exceeds 7-day demand → no loss."""
        df = _make_df([{
            "GoutCanon": "Original",
            "Quantité vendue": 10,
            "Volume vendu (hl)": 1.0,
            "Quantité disponible": 1000,
            "Volume disponible (hl)": 100.0,
        }])
        result = compute_losses_table_v48(df, 30, 400.0)
        assert len(result) == 1
        assert result.iloc[0]["Manque sur 7 j (hL)"] == 0.0
        assert result.iloc[0]["Perte (€)"] == 0.0

    def test_aggregation_by_gout(self):
        """Two formats of same flavor are aggregated."""
        df = _make_df([
            {
                "GoutCanon": "Pêche",
                "Volume vendu (hl)": 5.0,
                "Volume disponible (hl)": 0.5,
            },
            {
                "GoutCanon": "Pêche",
                "Volume vendu (hl)": 3.0,
                "Volume disponible (hl)": 0.3,
            },
        ])
        result = compute_losses_table_v48(df, 30, 400.0)
        assert len(result) == 1
        assert result.iloc[0]["Goût"] == "Pêche"

    def test_nan_gout_filtered(self):
        df = _make_df([{
            "GoutCanon": "nan",
            "Volume vendu (hl)": 5.0,
            "Volume disponible (hl)": 1.0,
        }])
        result = compute_losses_table_v48(df, 30, 400.0)
        assert result.empty

    def test_coffrets_filtered(self):
        df = _make_df([{
            "GoutCanon": "Autres (coffrets, goodies...)",
            "Volume vendu (hl)": 5.0,
            "Volume disponible (hl)": 1.0,
        }])
        result = compute_losses_table_v48(df, 30, 400.0)
        assert result.empty

    def test_window_days_minimum_one(self):
        """Window days < 1 should be treated as 1 to avoid division by zero."""
        df = _make_df([{
            "GoutCanon": "Test",
            "Volume vendu (hl)": 7.0,
            "Volume disponible (hl)": 0.0,
        }])
        result = compute_losses_table_v48(df, 0, 400.0)
        assert len(result) == 1
        # With 1 day window: demand 7j = 7/1 * 7 = 49 hL
        assert result.iloc[0]["Demande 7 j (hL)"] == 49.0

    def test_sorted_by_perte_descending(self):
        df = _make_df([
            {
                "GoutCanon": "Low",
                "Volume vendu (hl)": 1.0,
                "Volume disponible (hl)": 0.0,
            },
            {
                "GoutCanon": "High",
                "Volume vendu (hl)": 10.0,
                "Volume disponible (hl)": 0.0,
            },
        ])
        result = compute_losses_table_v48(df, 30, 400.0)
        assert result.iloc[0]["Goût"] == "High"

    def test_price_applied(self):
        df = _make_df([{
            "GoutCanon": "Test",
            "Volume vendu (hl)": 30.0,
            "Volume disponible (hl)": 0.0,
        }])
        result = compute_losses_table_v48(df, 30, 500.0)
        assert result.iloc[0]["Prix moyen (€/hL)"] == 500.0
