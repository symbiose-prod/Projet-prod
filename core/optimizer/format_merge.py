"""
core/optimizer/format_merge.py
==============================
Fusion des formats 12×33cl Symbiose dans les 6×33cl équivalents.

La marque Symbiose a basculé du carton 12×33cl vers 6×33cl. L'historique de
ventes EasyBeer continue à référencer les deux formats en parallèle, ce qui
pollue les propositions de production. NIKO conserve le 12×33cl.
"""
from __future__ import annotations

import logging

import pandas as pd

_log = logging.getLogger("ferment.optimizer.format_merge")

STOCK_6X33 = "Carton de 6 Bouteilles - 0.33L"
STOCK_12X33 = "Carton de 12 Bouteilles - 0.33L"


def _is_symbiose_product(produit: str) -> bool:
    p = str(produit or "").strip().upper()
    if p.startswith("NIKO"):
        return False
    if "IGEBA" in p:
        return False
    return True


def merge_symbiose_33cl(df: pd.DataFrame) -> pd.DataFrame:
    """Convertit les lignes Symbiose 12×33cl en équivalent 6×33cl et agrège."""
    if df is None or df.empty:
        return df
    if "Produit" not in df.columns or "Stock" not in df.columns:
        return df

    df = df.copy()
    stock_norm = df["Stock"].astype(str).str.strip()
    is_symbiose = df["Produit"].map(_is_symbiose_product)
    mask = is_symbiose & (stock_norm == STOCK_12X33)

    if not mask.any():
        return df

    _log.info("merge_symbiose_33cl: %d ligne(s) 12×33cl Symbiose converties en 6×33cl", int(mask.sum()))

    carton_cols = [c for c in ("Quantité disponible", "Quantité vendue") if c in df.columns]
    for c in carton_cols:
        df.loc[mask, c] = pd.to_numeric(df.loc[mask, c], errors="coerce").fillna(0.0) * 2

    df.loc[mask, "Stock"] = STOCK_6X33

    group_cols = [c for c in ("Produit", "Stock", "GoutCanon", "Produit_norm") if c in df.columns]
    agg: dict[str, str] = {}
    for c in df.columns:
        if c in group_cols:
            continue
        agg[c] = "sum" if pd.api.types.is_numeric_dtype(df[c]) else "first"

    return df.groupby(group_cols, as_index=False, sort=False).agg(agg).reset_index(drop=True)
