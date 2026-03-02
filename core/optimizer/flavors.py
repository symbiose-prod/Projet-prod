"""
core/optimizer/flavors.py
=========================
Flavor map loading, canonical flavor mapping, label sanitization.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .normalization import fix_text, _norm_colname, _pick_column


# ======= flavor map =========================================================
def load_flavor_map_from_path(path_csv: str) -> pd.DataFrame:
    import csv  # noqa: F401
    encodings = ["utf-8", "utf-8-sig", "cp1252", "latin1"]
    seps = [",", ";", "\t", "|"]
    if not Path(path_csv).exists():
        return pd.DataFrame(columns=["name", "canonical"])
    for enc in encodings:
        for sep in seps:
            try:
                fm = pd.read_csv(path_csv, encoding=enc, sep=sep, engine="python")
                lower = {c.lower(): c for c in fm.columns}
                if "name" in lower and "canonical" in lower:
                    fm = fm[[lower["name"], lower["canonical"]]].copy()
                    fm.columns = ["name", "canonical"]
                    fm = fm.dropna()
                    fm["name"] = fm["name"].astype(str).str.strip().map(fix_text)
                    fm["canonical"] = fm["canonical"].astype(str).str.strip().map(fix_text)
                    fm = fm[(fm["name"] != "") & (fm["canonical"] != "")]
                    return fm
            except Exception:
                continue
    return pd.DataFrame(columns=["name", "canonical"])


def apply_canonical_flavor(df: pd.DataFrame, fm: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    # 1) Trouve la colonne "Produit"
    prod_candidates = [
        "produit", "produit 1", "produit1", "produit 2",
        "designation", "désignation", "libelle", "libellé",
        "nom du produit", "product", "sku libelle", "sku libellé", "sku", "item",
    ]
    prod_candidates = [_norm_colname(x) for x in prod_candidates]
    col_prod = _pick_column(out, prod_candidates)

    if not col_prod:
        cols_list = ", ".join(map(str, out.columns))
        raise KeyError(
            "Colonne produit introuvable. "
            "Renomme la colonne en 'Produit' ou 'Désignation' (ou équivalent). "
            f"Colonnes détectées: {cols_list}"
        )

    # 2) Cree la colonne standard 'Produit'
    out["Produit"] = out[col_prod].astype(str).map(fix_text)
    out["Produit_norm"] = out["Produit"].str.strip()

    # 3) Mapping canonique
    if len(fm):
        fm = fm.dropna(subset=["name", "canonical"]).copy()
        fm["name_norm"] = fm["name"].astype(str).map(fix_text).str.strip().str.lower()
        fm["canonical"] = fm["canonical"].astype(str).map(fix_text).str.strip()
        m_exact = dict(zip(fm["name_norm"], fm["canonical"]))
        keys = list(m_exact.keys())
        import difflib as _difflib

        def to_canonical(prod: str) -> str:
            s = str(prod).strip().lower()
            if s in m_exact:
                return m_exact[s]
            try:
                close = _difflib.get_close_matches(s, keys, n=1, cutoff=0.92)
                if close:
                    return m_exact[close[0]]
            except Exception:
                pass
            return str(prod).strip()

        out["GoutCanon"] = out["Produit_norm"].map(to_canonical)
    else:
        out["GoutCanon"] = out["Produit_norm"]

    out["GoutCanon"] = out["GoutCanon"].astype(str).map(fix_text).str.strip()
    return out


BLOCKED_LABELS_EXACT = {"Autres (coffrets, goodies...)"}
BLOCKED_LABELS_LOWER = {"nan", "none", ""}


def sanitize_gouts(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["GoutCanon"] = out["GoutCanon"].astype(str).str.strip()
    mask = ~out["GoutCanon"].str.lower().isin(BLOCKED_LABELS_LOWER)
    mask &= ~out["GoutCanon"].isin(BLOCKED_LABELS_EXACT)
    return out.loc[mask].reset_index(drop=True)
