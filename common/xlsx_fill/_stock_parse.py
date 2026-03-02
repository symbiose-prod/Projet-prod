"""
common/xlsx_fill/_stock_parse.py
================================
Parse bottle format from Stock column, aggregate cartons/bottles from df_min.
"""
from __future__ import annotations

import re
from typing import Dict

import pandas as pd

from ._helpers import _is_close


def _parse_format_from_stock(stock: str):
    s = str(stock or "")
    m_nb = re.search(r"(Carton|Pack)\s+de\s+(\d+)\s+Bouteilles?", s, flags=re.I)
    nb = int(m_nb.group(2)) if m_nb else None
    m_l = re.search(r"(\d+(?:[.,]\d+)?)\s*[lL]\b", s)
    vol = float(m_l.group(1).replace(",", ".")) if m_l else None
    if vol is None:
        m_cl = re.search(r"(\d+(?:[.,]\d+)?)\s*c[lL]\b", s)
        vol = float(m_cl.group(1).replace(",", ".")) / 100.0 if m_cl else None
    return nb, vol


def _agg_from_dfmin(df_min: pd.DataFrame, gout: str) -> Dict[str, Dict[str, int]]:
    out = {
        "33_fr":   {"cartons": 0, "bouteilles": 0},
        "33_niko": {"cartons": 0, "bouteilles": 0},
        "75x6":    {"cartons": 0, "bouteilles": 0},
        "75x4":    {"cartons": 0, "bouteilles": 0},
    }
    if df_min is None or not isinstance(df_min, pd.DataFrame) or df_min.empty:
        return out
    req = {"Produit", "Stock", "GoutCanon", "Cartons \u00e0 produire (arrondi)", "Bouteilles \u00e0 produire (arrondi)"}
    if any(c not in df_min.columns for c in req):
        return out

    df = df_min.copy()
    df = df[df["GoutCanon"].astype(str).str.strip() == str(gout).strip()]
    if df.empty:
        return out

    for _, r in df.iterrows():
        nb, vol = _parse_format_from_stock(r["Stock"])
        if nb is None or vol is None:
            continue
        ct = int(pd.to_numeric(r["Cartons \u00e0 produire (arrondi)"], errors="coerce") or 0)
        bt = int(pd.to_numeric(r["Bouteilles \u00e0 produire (arrondi)"], errors="coerce") or 0)
        prod_up = str(r["Produit"]).upper()

        if nb == 12 and _is_close(vol, 0.33):
            key = "33_niko" if "NIKO" in prod_up else "33_fr"
        elif nb == 6 and _is_close(vol, 0.75):
            key = "75x6"
        elif nb == 4 and _is_close(vol, 0.75):
            key = "75x4"
        else:
            continue

        out[key]["cartons"] += ct
        out[key]["bouteilles"] += bt

    return out
