"""
tests/test_optimizer_format_merge.py
====================================
Fusion Symbiose 12×33cl → 6×33cl.
"""
from __future__ import annotations

import pandas as pd

from core.optimizer import merge_symbiose_33cl
from core.optimizer.format_merge import STOCK_6X33, STOCK_12X33


def _row(produit, stock, vendu, vol_vendu, dispo, vol_dispo, gout=""):
    return {
        "Produit": produit, "GoutCanon": gout or produit, "Stock": stock,
        "Quantité vendue": vendu, "Volume vendu (hl)": vol_vendu,
        "Quantité disponible": dispo, "Volume disponible (hl)": vol_dispo,
    }


def test_merge_symbiose_cumulative_case():
    # Cas utilisateur : 200 caisses 12× + 100 caisses 6× → 500 caisses 6×
    df = pd.DataFrame([
        _row("Kéfir Gingembre", STOCK_12X33, 200, 7.92, 80, 3.17, "Gingembre"),
        _row("Kéfir Gingembre", STOCK_6X33, 100, 1.98, 40, 0.79, "Gingembre"),
    ])
    out = merge_symbiose_33cl(df)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["Stock"] == STOCK_6X33
    assert row["Quantité vendue"] == 500.0
    assert row["Quantité disponible"] == 200.0
    assert abs(row["Volume vendu (hl)"] - 9.90) < 1e-6
    assert abs(row["Volume disponible (hl)"] - 3.96) < 1e-6


def test_merge_symbiose_12x33_only():
    # 12× seul → converti en 6× avec cartons ×2, volume inchangé
    df = pd.DataFrame([_row("Kéfir de fruits Original", STOCK_12X33, 100, 3.96, 50, 1.98)])
    out = merge_symbiose_33cl(df)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["Stock"] == STOCK_6X33
    assert row["Quantité vendue"] == 200.0
    assert row["Quantité disponible"] == 100.0
    assert abs(row["Volume vendu (hl)"] - 3.96) < 1e-6


def test_merge_leaves_niko_untouched():
    df = pd.DataFrame([_row("NIKO - Kéfir de fruits Pêche", STOCK_12X33, 300, 11.88, 60, 2.38)])
    out = merge_symbiose_33cl(df)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["Stock"] == STOCK_12X33
    assert row["Quantité vendue"] == 300


def test_merge_leaves_igeba_untouched():
    df = pd.DataFrame([_row("IGEBA Pêche", STOCK_12X33, 50, 1.98, 20, 0.79)])
    out = merge_symbiose_33cl(df)
    assert len(out) == 1
    assert out.iloc[0]["Stock"] == STOCK_12X33


def test_merge_leaves_non_33cl_untouched():
    df = pd.DataFrame([_row("Kéfir Pamplemousse", "Carton de 4 Bouteilles - 0.75L", 50, 1.50, 20, 0.60)])
    out = merge_symbiose_33cl(df)
    assert len(out) == 1
    assert out.iloc[0]["Stock"] == "Carton de 4 Bouteilles - 0.75L"


def test_merge_empty_or_missing_cols():
    assert merge_symbiose_33cl(pd.DataFrame()).empty
    df_no_stock = pd.DataFrame([{"Produit": "X"}])
    out = merge_symbiose_33cl(df_no_stock)
    assert "Produit" in out.columns
