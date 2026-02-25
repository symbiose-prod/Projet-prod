"""
common/biocoop.py
=================
Parsing et analytique des fichiers Biocoop :
  - Stats mensuelles (Evolution ventes par produit et par magasins)
  - Pre-commandes (fichier unique kefirs)
  - Segmentation magasins, penetration, ranking
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import Any

import pandas as pd

# ─── Catalogue produits ─────────────────────────────────────────────────────

INFUSION_CODES = {"FS5000", "FS5001", "FS5002", "FS5011"}
KEFIR_CODES = {"FS5003", "FS5004", "FS5005", "FS5006",
               "FS5007", "FS5008", "FS5009", "FS5010"}

PLATFORMS = ["CNE", "SE", "GO", "SO"]

CATEGORY_LABELS = {
    "Tous": None,
    "Infusions (ambiant)": INFUSION_CODES,
    "Kéfirs (frais)": KEFIR_CODES,
}


# ─── Helpers ────────────────────────────────────────────────────────────────

def _is_month(val: Any) -> bool:
    """True si la valeur ressemble a YYYY-MM."""
    return bool(re.match(r"^\d{4}-\d{2}$", str(val).strip()))


def _safe_str(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).strip()


def _safe_num(val: Any) -> float:
    try:
        v = pd.to_numeric(val, errors="coerce")
        return 0.0 if pd.isna(v) else float(v)
    except Exception:
        return 0.0


def _match_sheet(names: list[str], *keywords: str, exclude: str | None = None) -> str | None:
    """Trouve un nom d'onglet contenant tous les keywords (case-insensitive)."""
    for name in names:
        low = name.lower()
        if all(kw in low for kw in keywords):
            if exclude and exclude in low:
                continue
            return name
    return None


# ─── Parsing stats mensuelles ───────────────────────────────────────────────

def parse_monthly_stats(file_bytes: bytes) -> dict:
    """
    Parse le fichier Excel mensuel Biocoop (toutes les feuilles).

    Retourne :
        {
            "product_summary": DataFrame,  # par produit, metriques par mois
            "store_detail": DataFrame,     # par produit x magasin, qty par mois
            "non_ordering": DataFrame,     # magasins n'ayant pas commande
            "months": ["2026-01", ...]
        }
    """
    all_sheets = pd.read_excel(
        BytesIO(file_bytes) if isinstance(file_bytes, bytes) else file_bytes,
        sheet_name=None, header=None, engine="openpyxl",
    )
    names = list(all_sheets.keys())

    # Match sheets par mots-cles
    sh_product = _match_sheet(names, "produit", "par mois", exclude="magasin")
    sh_store = _match_sheet(names, "magasin", "moi")
    sh_noorder = _match_sheet(names, "pas command")

    product_summary = pd.DataFrame()
    store_detail = pd.DataFrame()
    non_ordering = pd.DataFrame()
    months: list[str] = []

    if sh_product:
        product_summary, months_p = _parse_product_summary(all_sheets[sh_product])
        product_summary = product_summary
        if not months:
            months = months_p

    if sh_store:
        store_detail, months_s = _parse_store_detail(all_sheets[sh_store])
        if not months:
            months = months_s

    if sh_noorder:
        non_ordering = _parse_non_ordering(all_sheets[sh_noorder])

    return {
        "product_summary": product_summary,
        "store_detail": store_detail,
        "non_ordering": non_ordering,
        "months": months,
    }


def _parse_product_summary(df_raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    Onglet 'Implant produit par mois'.
    Row 0 = marqueurs mois (YYYY-MM) a partir de col 13.
    Row 1 = headers detailles.
    Rows 2+ = donnees produits.
    Chaque mois = 3 colonnes (Qty, DN, VMM).
    """
    row0 = df_raw.iloc[0]
    months: list[str] = []
    month_col_starts: dict[str, int] = {}

    for col_idx in range(13, len(row0)):
        val = _safe_str(row0.iloc[col_idx])
        if _is_month(val) and val not in month_col_starts:
            months.append(val)
            month_col_starts[val] = col_idx

    records = []
    for i in range(2, len(df_raw)):
        row = df_raw.iloc[i]
        code_produit = _safe_str(row.iloc[4] if len(row) > 4 else row.iloc[1])
        col1_val = _safe_str(row.iloc[1]) if len(row) > 1 else ""
        if not code_produit or code_produit.lower().startswith("nombre") or "nombre" in col1_val.lower():
            continue
        # Skip lignes totaux (CodeProduit purement numerique = c'est un compteur)
        if code_produit.isdigit() and not code_produit.startswith("FS"):
            continue

        rec = {
            "CodeProduit": code_produit,
            "LibelleProduit": _safe_str(row.iloc[5] if len(row) > 5 else row.iloc[2]),
            "EAN": _safe_str(row.iloc[6] if len(row) > 6 else row.iloc[3]),
            "CumulN_qty": _safe_num(row.iloc[10] if len(row) > 10 else None),
            "CumulN_dn": _safe_num(row.iloc[11] if len(row) > 11 else None),
            "CumulN_vmm": _safe_num(row.iloc[12] if len(row) > 12 else None),
        }
        for m in months:
            base = month_col_starts[m]
            rec[f"{m}_qty"] = _safe_num(row.iloc[base] if len(row) > base else None)
            rec[f"{m}_dn"] = _safe_num(row.iloc[base + 1] if len(row) > base + 1 else None)
            rec[f"{m}_vmm"] = _safe_num(row.iloc[base + 2] if len(row) > base + 2 else None)
        records.append(rec)

    return pd.DataFrame(records), months


def _parse_store_detail(df_raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    Onglet 'Implant produit par magasin/mois'.
    Row 0 = marqueurs mois (YYYY-MM) a partir de col 11.
    Row 1 = headers.
    Rows 2+ = donnees produit x magasin.
    Chaque mois = 1 colonne (Qty).
    """
    row0 = df_raw.iloc[0]
    months: list[str] = []
    month_cols: dict[str, int] = {}

    for col_idx in range(11, len(row0)):
        val = _safe_str(row0.iloc[col_idx])
        if _is_month(val) and val not in month_cols:
            months.append(val)
            month_cols[val] = col_idx

    records = []
    for i in range(2, len(df_raw)):
        row = df_raw.iloc[i]
        code_produit = _safe_str(row.iloc[1])
        code_client = _safe_str(row.iloc[4])
        if not code_produit or not code_client:
            continue
        if "nombre" in code_produit.lower() or (code_produit.isdigit() and not code_produit.startswith("FS")):
            continue

        rec = {
            "CodeProduit": code_produit,
            "LibelleProduit": _safe_str(row.iloc[2]),
            "CodeClient": code_client,
            "NomClient": _safe_str(row.iloc[5]),
            "CP": _safe_str(row.iloc[6]),
            "Ville": _safe_str(row.iloc[7]),
            "Plateforme": _safe_str(row.iloc[8]),
            "CumulN_qty": _safe_num(row.iloc[10] if len(row) > 10 else None),
        }
        for m in months:
            rec[f"{m}_qty"] = _safe_num(row.iloc[month_cols[m]] if len(row) > month_cols[m] else None)
        records.append(rec)

    return pd.DataFrame(records), months


def _parse_non_ordering(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Onglet 'Liste magasins pas commande produit'.
    Colonnes utiles par position (les autres sont vides) :
    [0]=CodeProduit, [1]=Libelle, [2]=CodeClient, [3]=NomClient,
    [7]=CP, [8]=Ville, [12]=Plateforme.
    """
    records = []
    for i in range(2, len(df_raw)):
        row = df_raw.iloc[i]
        code_produit = _safe_str(row.iloc[0])
        code_client = _safe_str(row.iloc[2])
        if not code_produit or not code_client:
            continue

        records.append({
            "CodeProduit": code_produit,
            "LibelleProduit": _safe_str(row.iloc[1]),
            "CodeClient": code_client,
            "NomClient": _safe_str(row.iloc[3]),
            "CP": _safe_str(row.iloc[7]) if len(row) > 7 else "",
            "Ville": _safe_str(row.iloc[8]) if len(row) > 8 else "",
            "Plateforme": _safe_str(row.iloc[12]) if len(row) > 12 else "",
        })
    return pd.DataFrame(records)


# ─── Parsing pre-commandes ──────────────────────────────────────────────────

def parse_precommandes(file_bytes: bytes) -> pd.DataFrame:
    """
    Parse le fichier de pre-commandes kefir Biocoop.

    Row 1 (idx 1) = codes magasin depuis col 7.
    Row 3 (idx 3) = noms magasin depuis col 7.
    Rows 4-11 = 8 produits kefir.

    Retourne un DataFrame en format long :
        CodeProduit, Designation, CodeClient, NomClient, Qty
    """
    df_raw = pd.read_excel(
        BytesIO(file_bytes) if isinstance(file_bytes, bytes) else file_bytes,
        header=None, engine="openpyxl",
    )

    # Trouver la fin des colonnes magasins (exclure les colonnes de totaux)
    row3 = df_raw.iloc[3]
    last_store_col = 7
    for col_idx in range(7, len(row3)):
        val = _safe_str(row3.iloc[col_idx])
        if val and "vente" not in val.lower() and val.upper().startswith("BIOCOOP") or (val and "BIOCOOP" not in val.upper() and val and not val.lower().startswith("vente") and col_idx < len(row3) - 4):
            last_store_col = col_idx
        else:
            if val and ("vente" in val.lower() or val == ""):
                break
            last_store_col = col_idx

    # Approche plus robuste : utiliser row 1 (codes magasin)
    store_codes = []
    store_names = []
    for col_idx in range(7, len(df_raw.iloc[1])):
        code = _safe_str(df_raw.iloc[1].iloc[col_idx])
        name = _safe_str(df_raw.iloc[3].iloc[col_idx]) if len(df_raw.iloc[3]) > col_idx else ""
        if not code:
            break
        store_codes.append(code)
        store_names.append(name)

    records = []
    for prod_idx in range(4, 12):
        if prod_idx >= len(df_raw):
            break
        row = df_raw.iloc[prod_idx]
        code_produit = _safe_str(row.iloc[2])
        designation = _safe_str(row.iloc[3])
        if not code_produit:
            continue

        for offset, (s_code, s_name) in enumerate(zip(store_codes, store_names)):
            col = 7 + offset
            if col >= len(row):
                break
            qty = _safe_num(row.iloc[col])
            if qty > 0:
                records.append({
                    "CodeProduit": code_produit,
                    "Designation": designation,
                    "CodeClient": s_code,
                    "NomClient": s_name,
                    "Qty": int(qty),
                })

    return pd.DataFrame(records)


# ─── Filtres ────────────────────────────────────────────────────────────────

def filter_by_category(df: pd.DataFrame, category: str) -> pd.DataFrame:
    """Filtre un DataFrame sur la categorie produit."""
    codes = CATEGORY_LABELS.get(category)
    if codes is None or df.empty or "CodeProduit" not in df.columns:
        return df
    return df[df["CodeProduit"].isin(codes)].copy()


def filter_by_platform(df: pd.DataFrame, platforms: list[str]) -> pd.DataFrame:
    """Filtre un DataFrame sur les plateformes selectionnees."""
    if not platforms or df.empty or "Plateforme" not in df.columns:
        return df
    return df[df["Plateforme"].isin(platforms)].copy()


# ─── Analytique ─────────────────────────────────────────────────────────────

def compute_penetration(
    store_detail: pd.DataFrame,
    non_ordering: pd.DataFrame,
    months: list[str],
    product_codes: set[str] | None = None,
) -> pd.DataFrame:
    """
    Calcule le taux de penetration par produit x plateforme.
    Retourne : CodeProduit, LibelleProduit, Plateforme, TotalMagasins, MagasinsActifs, PenetrationPct
    """
    if store_detail.empty:
        return pd.DataFrame()

    latest = months[-1] if months else None
    qty_col = f"{latest}_qty" if latest else None

    # Magasins actifs (detail)
    sd = store_detail.copy()
    if product_codes:
        sd = sd[sd["CodeProduit"].isin(product_codes)]

    # Magasins non-clients
    no = non_ordering.copy()
    if product_codes:
        no = no[no["CodeProduit"].isin(product_codes)]

    # Combiner pour avoir l'univers total
    all_combos = pd.concat([
        sd[["CodeProduit", "LibelleProduit", "CodeClient", "Plateforme"]],
        no[["CodeProduit", "LibelleProduit", "CodeClient", "Plateforme"]],
    ], ignore_index=True).drop_duplicates(subset=["CodeProduit", "CodeClient"])

    # Total par produit x plateforme
    total = all_combos.groupby(["CodeProduit", "Plateforme"]).size().reset_index(name="TotalMagasins")

    # Actifs (dernier mois qty > 0)
    if qty_col and qty_col in sd.columns:
        actifs = sd[sd[qty_col] > 0].drop_duplicates(subset=["CodeProduit", "CodeClient"])
        actifs_count = actifs.groupby(["CodeProduit", "Plateforme"]).size().reset_index(name="MagasinsActifs")
    else:
        actifs_count = pd.DataFrame(columns=["CodeProduit", "Plateforme", "MagasinsActifs"])

    result = total.merge(actifs_count, on=["CodeProduit", "Plateforme"], how="left")
    result["MagasinsActifs"] = result["MagasinsActifs"].fillna(0).astype(int)
    result["PenetrationPct"] = (result["MagasinsActifs"] / result["TotalMagasins"] * 100).round(1)

    # Ajouter libelle
    labels = all_combos.drop_duplicates("CodeProduit")[["CodeProduit", "LibelleProduit"]]
    result = result.merge(labels, on="CodeProduit", how="left")

    return result.sort_values(["CodeProduit", "Plateforme"]).reset_index(drop=True)


def compute_store_ranking(
    store_detail: pd.DataFrame,
    months: list[str],
) -> pd.DataFrame:
    """
    Classement des magasins par volume total.
    Retourne : CodeClient, NomClient, Ville, CP, Plateforme, TotalQty, NbProduits, QtyDernierMois
    """
    if store_detail.empty or not months:
        return pd.DataFrame()

    latest = months[-1]
    qty_cols = [f"{m}_qty" for m in months if f"{m}_qty" in store_detail.columns]
    latest_col = f"{latest}_qty"

    df = store_detail.copy()

    # Total qty toutes periodes
    df["_total_qty"] = df[qty_cols].sum(axis=1)

    # Agreger par magasin
    agg = df.groupby(["CodeClient", "NomClient", "Ville", "CP", "Plateforme"]).agg(
        TotalQty=("_total_qty", "sum"),
        NbProduits=("CodeProduit", "nunique"),
        QtyDernierMois=(latest_col, "sum") if latest_col in df.columns else ("_total_qty", "sum"),
    ).reset_index()

    agg["TotalQty"] = agg["TotalQty"].astype(int)
    agg["QtyDernierMois"] = agg["QtyDernierMois"].astype(int)

    return agg.sort_values("TotalQty", ascending=False).reset_index(drop=True)


def compare_preorder_vs_actual(
    preorder_df: pd.DataFrame,
    store_detail: pd.DataFrame,
    months: list[str],
) -> pd.DataFrame:
    """
    Compare pre-commandes kefir avec ventes reelles.
    Retourne par magasin : QtyPrecommande, QtyTotale, QtyDernierMois, Statut
    """
    if preorder_df.empty or store_detail.empty or not months:
        return pd.DataFrame()

    latest = months[-1]
    qty_cols = [f"{m}_qty" for m in months if f"{m}_qty" in store_detail.columns]
    latest_col = f"{latest}_qty"

    # Pre-commandes par magasin
    pre_agg = preorder_df.groupby(["CodeClient", "NomClient"]).agg(
        QtyPrecommande=("Qty", "sum")
    ).reset_index()

    # Ventes reelles par magasin (kefirs seulement)
    sd_kefir = store_detail[store_detail["CodeProduit"].isin(KEFIR_CODES)].copy()
    if sd_kefir.empty:
        return pd.DataFrame()

    sd_kefir["_total"] = sd_kefir[qty_cols].sum(axis=1)
    sd_kefir["_latest"] = sd_kefir[latest_col] if latest_col in sd_kefir.columns else 0

    actual_agg = sd_kefir.groupby("CodeClient").agg(
        QtyTotale=("_total", "sum"),
        QtyDernierMois=("_latest", "sum"),
    ).reset_index()

    # Merge
    result = pre_agg.merge(actual_agg, on="CodeClient", how="outer")
    result["QtyPrecommande"] = result["QtyPrecommande"].fillna(0).astype(int)
    result["QtyTotale"] = result["QtyTotale"].fillna(0).astype(int)
    result["QtyDernierMois"] = result["QtyDernierMois"].fillna(0).astype(int)

    # Remplir NomClient depuis store_detail si manquant
    if "NomClient" not in result.columns or result["NomClient"].isna().any():
        sd_names = sd_kefir.drop_duplicates("CodeClient")[["CodeClient", "NomClient"]].rename(
            columns={"NomClient": "_nm"}
        )
        result = result.merge(sd_names, on="CodeClient", how="left")
        result["NomClient"] = result["NomClient"].fillna(result.get("_nm", ""))
        result.drop(columns=["_nm"], errors="ignore", inplace=True)

    # Statut
    def _status(row):
        pre = row["QtyPrecommande"] > 0
        latest = row["QtyDernierMois"] > 0
        ever = row["QtyTotale"] > 0
        if pre and latest:
            return "\U0001F7E2 Actifs confirmés"
        if pre and ever:
            return "\U0001F7E0 À surveiller"
        if pre and not ever:
            return "\U0001F534 Perdus"
        if not pre and latest:
            return "\U0001F535 Nouveaux"
        return "\u26AA Non-clients"

    result["Statut"] = result.apply(_status, axis=1)

    return result.sort_values("QtyTotale", ascending=False).reset_index(drop=True)


def segment_stores(
    store_detail: pd.DataFrame,
    non_ordering: pd.DataFrame,
    preorder_df: pd.DataFrame | None = None,
    months: list[str] | None = None,
    category: str = "kefir",
) -> pd.DataFrame:
    """
    Segmente les magasins en groupes actionnables.

    Kefir (avec pre-commandes) :
      - Actifs confirmes, A surveiller, Perdus, Nouveaux, Non-clients
    Infusions (sans pre-commandes) :
      - Actifs, Intermittents, Non-clients
    """
    if store_detail.empty:
        return pd.DataFrame()

    latest = months[-1] if months else None
    qty_col = f"{latest}_qty" if latest else None

    # Ensembles de magasins
    ever_buyers: set[str] = set()
    latest_buyers: set[str] = set()

    for m in (months or []):
        col = f"{m}_qty"
        if col in store_detail.columns:
            buyers = store_detail[store_detail[col] > 0]["CodeClient"].unique()
            ever_buyers.update(buyers)

    if qty_col and qty_col in store_detail.columns:
        latest_buyers = set(store_detail[store_detail[qty_col] > 0]["CodeClient"].unique())

    all_non_clients = set(non_ordering["CodeClient"].unique()) if not non_ordering.empty else set()

    rows = []
    if category == "kefir" and preorder_df is not None and not preorder_df.empty:
        preorder_stores = set(preorder_df["CodeClient"].unique())
        all_codes = preorder_stores | ever_buyers | all_non_clients

        for code in all_codes:
            in_pre = code in preorder_stores
            in_latest = code in latest_buyers
            in_ever = code in ever_buyers

            if in_pre and in_latest:
                seg = "\U0001F7E2 Actifs confirmés"
            elif in_pre and in_ever:
                seg = "\U0001F7E0 À surveiller"
            elif in_pre:
                seg = "\U0001F534 Perdus"
            elif in_latest:
                seg = "\U0001F535 Nouveaux"
            else:
                seg = "\u26AA Non-clients"
            rows.append({"CodeClient": code, "Segment": seg})
    else:
        all_codes = ever_buyers | all_non_clients
        for code in all_codes:
            if code in latest_buyers:
                seg = "\U0001F7E2 Actifs"
            elif code in ever_buyers and len(months or []) > 1:
                seg = "\U0001F7E0 Intermittents"
            elif code in ever_buyers:
                seg = "\U0001F7E2 Actifs"
            else:
                seg = "\u26AA Non-clients"
            rows.append({"CodeClient": code, "Segment": seg})

    return pd.DataFrame(rows)
