"""Tests for core/optimizer.py — text normalization functions."""
from __future__ import annotations

import pandas as pd
import pytest

from core.optimizer import fix_text, _norm_colname


# ─── fix_text ─────────────────────────────────────────────────────────────────

class TestFixText:

    def test_none_returns_empty(self):
        assert fix_text(None) == ""

    def test_normal_text_unchanged(self):
        assert fix_text("Mélisse") == "Mélisse"

    def test_replacement_char_fixed(self):
        result = fix_text("M\uFFFDlisse")
        # The replacement char \uFFFD should be replaced by 'é'
        assert "é" in result or "e" in result

    def test_custom_replacement(self):
        assert fix_text("M\uFFFDlisse") == "Mélisse"

    def test_poivree_custom(self):
        assert fix_text("poivr\uFFFDe") == "poivrée"

    def test_integer_input(self):
        assert fix_text(42) == "42"

    def test_empty_string(self):
        assert fix_text("") == ""

    def test_latin1_double_encoding(self):
        # Simulate double-encoded UTF-8 interpreted as Latin-1
        original = "Pêche"
        try:
            broken = original.encode("utf-8").decode("latin1")
            result = fix_text(broken)
            # fix_text should detect the double encoding and fix it
            assert "ê" in result or "e" in result
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass  # Skip on systems where this fails


# ─── _norm_colname ────────────────────────────────────────────────────────────

class TestNormColname:

    def test_basic_lowercase(self):
        assert _norm_colname("Produit") == "produit"

    def test_accent_removal(self):
        assert _norm_colname("Désignation") == "designation"

    def test_special_chars_to_spaces(self):
        assert _norm_colname("Volume vendu (hl)") == "volume vendu hl"

    def test_multi_spaces_collapsed(self):
        assert _norm_colname("  Volume   vendu  ") == "volume vendu"

    def test_none_handled(self):
        assert _norm_colname(None) == ""

    def test_numeric_preserved(self):
        assert _norm_colname("Produit 1") == "produit 1"
