"""
core/optimizer/losses.py
========================
Loss/shortage computation table.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .normalization import fix_text


def compute_losses_table_v48(df_in_all: pd.DataFrame, window_days: float, price_hL: float) -> pd.DataFrame:
    out_cols = [
        "Goût", "Demande 7 j (hL)", "Stock (hL)",
        "Manque sur 7 j (hL)", "Prix moyen (€/hL)", "Perte (€)",
    ]
    if df_in_all is None or not isinstance(df_in_all, pd.DataFrame) or df_in_all.empty:
        return pd.DataFrame(columns=out_cols)
    df = df_in_all.copy()
    if "GoutCanon" not in df.columns:
        return pd.DataFrame(columns=out_cols)
    for c in ["Quantité vendue", "Volume vendu (hl)", "Quantité disponible", "Volume disponible (hl)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["GoutCanon"] = df["GoutCanon"].astype(str).str.strip()
    bad_lower = {"nan", "none", ""}
    df = df[~df["GoutCanon"].str.lower().isin(bad_lower)]
    df = df[df["GoutCanon"] != "Autres (coffrets, goodies...)"]
    if df.empty:
        return pd.DataFrame(columns=out_cols)
    jours = max(float(window_days), 1.0)

    # --- Calcul par ligne (gout + format) ---
    df["vitesse_hL_j"] = df["Volume vendu (hl)"] / jours
    df["Demande 7 j (hL)"] = 7.0 * df["vitesse_hL_j"]
    df["Stock (hL)"] = df["Volume disponible (hl)"]
    df["Manque_ligne"] = np.clip(
        df["Demande 7 j (hL)"] - df["Stock (hL)"],
        a_min=0.0,
        a_max=None,
    )

    # --- Agregation par Gout ---
    agg = df.groupby("GoutCanon", as_index=False).agg(
        demande_7j=("Demande 7 j (hL)", "sum"),
        stock_hL=("Stock (hL)", "sum"),
        manque_7j=("Manque_ligne", "sum"),
    )
    if agg.empty:
        return pd.DataFrame(columns=out_cols)

    agg["Prix moyen (€/hL)"] = float(price_hL)
    agg["Perte (€)"] = (agg["manque_7j"] * agg["Prix moyen (€/hL)"]).round(0)

    pertes = agg.rename(
        columns={
            "GoutCanon": "Goût",
            "demande_7j": "Demande 7 j (hL)",
            "stock_hL": "Stock (hL)",
            "manque_7j": "Manque sur 7 j (hL)",
        }
    )[out_cols]
    pertes["Goût"] = pertes["Goût"].map(fix_text)
    pertes["Demande 7 j (hL)"] = pertes["Demande 7 j (hL)"].round(2)
    pertes["Stock (hL)"] = pertes["Stock (hL)"].round(2)
    pertes["Manque sur 7 j (hL)"] = pertes["Manque sur 7 j (hL)"].round(2)
    pertes["Prix moyen (€/hL)"] = pertes["Prix moyen (€/hL)"].round(0)
    return pertes.sort_values("Perte (€)", ascending=False).reset_index(drop=True)
