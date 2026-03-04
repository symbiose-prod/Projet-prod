"""Tests for common/session_store.py — zlib compression for session DataFrames."""
from __future__ import annotations

import pandas as pd

from common.session_store import load_df, store_df


class TestSessionStore:

    def test_roundtrip(self):
        df = pd.DataFrame({"A": [1, 2, 3], "B": ["x", "y", "z"]})
        stored = store_df(df)
        recovered = load_df(stored)
        pd.testing.assert_frame_equal(df, recovered)

    def test_compressed_prefix(self):
        df = pd.DataFrame({"A": [1]})
        stored = store_df(df)
        assert stored.startswith("zlib:")

    def test_backward_compat_raw_json(self):
        """load_df must handle uncompressed JSON from older sessions."""
        df = pd.DataFrame({"A": [1, 2, 3]})
        raw = df.to_json(orient="split")
        recovered = load_df(raw)
        pd.testing.assert_frame_equal(df, recovered)

    def test_empty_string_returns_empty_df(self):
        result = load_df("")
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_compression_ratio(self):
        # Realistic data — should achieve at least 40% compression
        df = pd.DataFrame({
            "Produit": [f"Kéfir Goût {i}" for i in range(200)],
            "Volume": list(range(200)),
            "Stock": ["Carton de 12 - 33cl" for _ in range(200)],
        })
        raw_json = df.to_json(orient="split")
        stored = store_df(df)
        assert len(stored) < len(raw_json) * 0.6

    def test_large_df_roundtrip(self):
        df = pd.DataFrame({
            f"col_{i}": list(range(500))
            for i in range(20)
        })
        stored = store_df(df)
        recovered = load_df(stored)
        pd.testing.assert_frame_equal(df, recovered)
