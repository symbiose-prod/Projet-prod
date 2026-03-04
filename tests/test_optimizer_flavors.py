"""Tests for core/optimizer/flavors.py — flavor map, canonical mapping, sanitization."""
from __future__ import annotations

import pandas as pd
import pytest

from core.optimizer.flavors import (
    apply_canonical_flavor,
    load_flavor_map_from_path,
    sanitize_gouts,
)

# ─── helpers ─────────────────────────────────────────────────────────────────


def _make_flavor_map(rows: list[tuple[str, str]]) -> pd.DataFrame:
    """Build a minimal flavor map DataFrame."""
    return pd.DataFrame(rows, columns=["name", "canonical"])


def _make_product_df(values: list[str], col: str = "Produit") -> pd.DataFrame:
    """Build a one-column product DataFrame."""
    return pd.DataFrame({col: values})


def _make_gout_df(values: list[str]) -> pd.DataFrame:
    """Build a DataFrame with a GoutCanon column for sanitize_gouts."""
    return pd.DataFrame({"GoutCanon": values})


# ─── TestLoadFlavorMapFromPath ───────────────────────────────────────────────


class TestLoadFlavorMapFromPath:

    def test_missing_file_returns_empty(self, tmp_path):
        result = load_flavor_map_from_path(str(tmp_path / "nonexistent.csv"))
        assert isinstance(result, pd.DataFrame)
        assert list(result.columns) == ["name", "canonical"]
        assert len(result) == 0

    def test_valid_csv_comma_separated(self, tmp_path):
        csv_file = tmp_path / "flavors.csv"
        csv_file.write_text("name,canonical\nOriginal,Kefir Original\nPeche,Kefir Peche\n", encoding="utf-8")
        result = load_flavor_map_from_path(str(csv_file))
        assert len(result) == 2
        assert list(result.columns) == ["name", "canonical"]
        assert result.iloc[0]["name"] == "Original"
        assert result.iloc[0]["canonical"] == "Kefir Original"
        assert result.iloc[1]["name"] == "Peche"

    def test_semicolon_separator(self, tmp_path):
        csv_file = tmp_path / "flavors.csv"
        csv_file.write_text("name;canonical\nOriginal;Kefir Original\nMenthe;Kefir Menthe\n", encoding="utf-8")
        result = load_flavor_map_from_path(str(csv_file))
        assert len(result) == 2
        assert result.iloc[0]["name"] == "Original"
        assert result.iloc[1]["canonical"] == "Kefir Menthe"

    def test_missing_required_columns_returns_empty(self, tmp_path):
        csv_file = tmp_path / "flavors.csv"
        csv_file.write_text("flavor,mapping\nOriginal,Kefir Original\n", encoding="utf-8")
        result = load_flavor_map_from_path(str(csv_file))
        assert isinstance(result, pd.DataFrame)
        assert list(result.columns) == ["name", "canonical"]
        assert len(result) == 0

    def test_nan_and_empty_rows_filtered_out(self, tmp_path):
        csv_file = tmp_path / "flavors.csv"
        content = "name,canonical\nOriginal,Kefir Original\n,\n ,  \nPeche,Kefir Peche\n"
        csv_file.write_text(content, encoding="utf-8")
        result = load_flavor_map_from_path(str(csv_file))
        assert len(result) == 2
        names = result["name"].tolist()
        assert "Original" in names
        assert "Peche" in names

    def test_case_insensitive_column_detection(self, tmp_path):
        csv_file = tmp_path / "flavors.csv"
        csv_file.write_text("Name,Canonical\nOriginal,Kefir Original\n", encoding="utf-8")
        result = load_flavor_map_from_path(str(csv_file))
        assert len(result) == 1
        assert result.iloc[0]["name"] == "Original"


# ─── TestApplyCanonicalFlavor ────────────────────────────────────────────────


class TestApplyCanonicalFlavor:

    def test_exact_mapping_with_flavor_map(self):
        df = _make_product_df(["Original", "Peche", "Menthe"])
        fm = _make_flavor_map([
            ("Original", "Kefir Original"),
            ("Peche", "Kefir Peche"),
            ("Menthe", "Kefir Menthe"),
        ])
        result = apply_canonical_flavor(df, fm)
        assert "GoutCanon" in result.columns
        assert result["GoutCanon"].iloc[0] == "Kefir Original"
        assert result["GoutCanon"].iloc[1] == "Kefir Peche"
        assert result["GoutCanon"].iloc[2] == "Kefir Menthe"

    def test_empty_flavor_map_uses_produit_norm(self):
        df = _make_product_df(["Original", "Peche"])
        fm = pd.DataFrame(columns=["name", "canonical"])
        result = apply_canonical_flavor(df, fm)
        assert "GoutCanon" in result.columns
        assert result["GoutCanon"].iloc[0] == "Original"
        assert result["GoutCanon"].iloc[1] == "Peche"

    def test_column_named_produit(self):
        df = _make_product_df(["Original"], col="Produit")
        fm = _make_flavor_map([("Original", "Kefir Original")])
        result = apply_canonical_flavor(df, fm)
        assert result["GoutCanon"].iloc[0] == "Kefir Original"

    def test_column_named_designation(self):
        df = _make_product_df(["Original"], col="Désignation")
        fm = _make_flavor_map([("Original", "Kefir Original")])
        result = apply_canonical_flavor(df, fm)
        assert result["GoutCanon"].iloc[0] == "Kefir Original"

    def test_column_named_product(self):
        df = _make_product_df(["Original"], col="Product")
        fm = _make_flavor_map([("Original", "Kefir Original")])
        result = apply_canonical_flavor(df, fm)
        assert result["GoutCanon"].iloc[0] == "Kefir Original"

    def test_no_recognized_column_raises_keyerror(self):
        df = pd.DataFrame({"Prix": [10], "Quantite": [5]})
        fm = _make_flavor_map([("Original", "Kefir Original")])
        with pytest.raises(KeyError, match="Colonne produit introuvable"):
            apply_canonical_flavor(df, fm)

    def test_fuzzy_matching_close_name(self):
        """A name very close to a flavor map entry (>=0.92 similarity) should match."""
        df = _make_product_df(["Kefir Originall"])  # extra 'l' — very close
        fm = _make_flavor_map([("Kefir Originall", "Kefir Original")])
        result = apply_canonical_flavor(df, fm)
        # Exact match (the name in fm is "Kefir Originall" which matches exactly)
        assert result["GoutCanon"].iloc[0] == "Kefir Original"

    def test_fuzzy_matching_slight_typo(self):
        """A slight typo that passes the 0.92 cutoff should still resolve via difflib."""
        # "Kefir Origina" vs "Kefir Original" — similarity ~0.96
        df = _make_product_df(["Kefir Origina"])
        fm = _make_flavor_map([("Kefir Original", "Kefir Original Canon")])
        result = apply_canonical_flavor(df, fm)
        assert result["GoutCanon"].iloc[0] == "Kefir Original Canon"

    def test_no_match_returns_original(self):
        """A name that does not match anything in the flavor map is returned as-is."""
        df = _make_product_df(["Completely Different Product"])
        fm = _make_flavor_map([("Original", "Kefir Original")])
        result = apply_canonical_flavor(df, fm)
        assert result["GoutCanon"].iloc[0] == "Completely Different Product"

    def test_does_not_mutate_input(self):
        df = _make_product_df(["Original", "Peche"])
        fm = _make_flavor_map([("Original", "Kefir Original")])
        original_cols = list(df.columns)
        apply_canonical_flavor(df, fm)
        assert list(df.columns) == original_cols
        assert "GoutCanon" not in df.columns


# ─── TestSanitizeGouts ───────────────────────────────────────────────────────


class TestSanitizeGouts:

    def test_filters_nan(self):
        df = _make_gout_df(["Kefir Original", "nan", "Kefir Peche"])
        result = sanitize_gouts(df)
        assert len(result) == 2
        assert "nan" not in result["GoutCanon"].values

    def test_filters_none(self):
        df = _make_gout_df(["Kefir Original", "None", "Kefir Peche"])
        result = sanitize_gouts(df)
        assert len(result) == 2
        assert "None" not in result["GoutCanon"].values

    def test_filters_empty_string(self):
        df = _make_gout_df(["Kefir Original", "", "Kefir Peche"])
        result = sanitize_gouts(df)
        assert len(result) == 2
        assert "" not in result["GoutCanon"].values

    def test_filters_blocked_exact_label(self):
        df = _make_gout_df(["Kefir Original", "Autres (coffrets, goodies...)", "Kefir Peche"])
        result = sanitize_gouts(df)
        assert len(result) == 2
        assert "Autres (coffrets, goodies...)" not in result["GoutCanon"].values

    def test_keeps_valid_gouts(self):
        valid = ["Kefir Original", "Kefir Peche", "Kefir Menthe"]
        df = _make_gout_df(valid)
        result = sanitize_gouts(df)
        assert len(result) == 3
        assert result["GoutCanon"].tolist() == valid

    def test_resets_index_after_filter(self):
        df = _make_gout_df(["nan", "Kefir Original", "none", "Kefir Peche"])
        result = sanitize_gouts(df)
        assert list(result.index) == [0, 1]

    def test_does_not_mutate_input(self):
        df = _make_gout_df(["Kefir Original", "nan"])
        original_len = len(df)
        sanitize_gouts(df)
        assert len(df) == original_len
