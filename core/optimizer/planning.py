"""
core/optimizer/planning.py
==========================
Production planning: compute_plan + equalization helpers.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common.data import get_business_config as _get_biz

from .parsing import is_allowed_format, parse_stock, safe_num

# ======= constantes ==========================================================
ROUND_TO_CARTON = True
EPS = 1e-9
PRICE_REF_HL: float = _get_biz().get("price_ref_hl", 400.0)


# ---------- Helpers selection et egalisation ----------

def _weekly_perte(stock_hl: float, vitesse_hl_j: float, price_hL: float = PRICE_REF_HL) -> float:
    """Perte euro sur 7 jours si on ne produit pas : max(demande7 - stock, 0) * prix."""
    dem7 = 7.0 * max(float(vitesse_hl_j), 0.0)
    manque = max(dem7 - max(float(stock_hl), 0.0), 0.0)
    return manque * float(price_hL)


def _equalize_last_batch_global(Gi: np.ndarray, vi: np.ndarray, V: float) -> np.ndarray:
    """
    Egalise un horizon unique T sur T = (Gi + xi)/vi pour un groupe donne.
    Resout sum_i max(0, T*vi - Gi) = V par dichotomie (xi >= 0).
    Retourne x (hL) par ligne.
    """
    vi = np.maximum(vi.astype(float), 0.0)
    Gi = np.maximum(Gi.astype(float), 0.0)
    if V <= 1e-12 or vi.sum() <= 1e-12:
        return np.zeros_like(Gi)

    # bornes : T_min = max(Gi/vi) (horizon sans prod), T_max = T_min + marge
    with np.errstate(divide="ignore", invalid="ignore"):
        T0 = np.nanmax(np.where(vi > 0, Gi / np.maximum(vi, 1e-12), 0.0))
    T_lo = T0
    T_hi = T0 + (V / max(np.max(vi), 1e-12)) + 365.0  # marge large

    # dichotomie
    for _ in range(80):
        T_mid = 0.5 * (T_lo + T_hi)
        x = np.maximum(T_mid * vi - Gi, 0.0)
        s = x.sum()
        if s > V:
            T_hi = T_mid
        else:
            T_lo = T_mid
    x = np.maximum(T_lo * vi - Gi, 0.0)

    # petit rescale pour coller a V
    s = x.sum()
    if s > 0:
        x *= V / s
    return x


# ======= calcul principal ====================================================

def compute_plan(df_in, window_days, volume_cible, nb_gouts, repartir_pro_rv, manual_keep, exclude_list):
    required = [
        "Produit", "GoutCanon", "Stock", "Quantité vendue",
        "Volume vendu (hl)", "Quantité disponible", "Volume disponible (hl)",
    ]
    miss = [c for c in required if c not in df_in.columns]
    if miss:
        raise ValueError(f"Colonnes manquantes: {miss}")

    # --- helper categorie ---
    def _category(g: str) -> str:
        s = str(g or "").strip().lower()
        return "infusion" if "infusion" in s else "kefir"

    note_msg = ""  # message d'ajustement a renvoyer a l'UI

    df = df_in[required].copy()
    for c in ["Quantité vendue", "Volume vendu (hl)", "Quantité disponible", "Volume disponible (hl)"]:
        df[c] = safe_num(df[c])

    parsed = df["Stock"].apply(parse_stock)
    df[["Bouteilles/carton", "Volume bouteille (L)"]] = pd.DataFrame(parsed.tolist(), index=df.index)
    mask_allowed = df.apply(
        lambda r: is_allowed_format(r["Bouteilles/carton"], r["Volume bouteille (L)"], str(r["Stock"])),
        axis=1,
    )
    df = df.loc[mask_allowed].reset_index(drop=True)

    df["Volume/carton (hL)"] = (df["Bouteilles/carton"] * df["Volume bouteille (L)"]) / 100.0
    df = df.dropna(
        subset=["GoutCanon", "Volume/carton (hL)", "Volume vendu (hl)", "Volume disponible (hl)"]
    ).reset_index(drop=True)

    df_all_formats = df.copy()  # noqa: F841

    if exclude_list:
        ex = {s.strip() for s in exclude_list}
        df = df[~df["GoutCanon"].astype(str).str.strip().isin(ex)]

    if manual_keep:
        keep = {g.strip() for g in manual_keep}
        df = df[df["GoutCanon"].astype(str).str.strip().isin(keep)]

    # --- agregats par gout ---
    jours = max(float(window_days), 1.0)

    agg = df.groupby("GoutCanon").agg(
        ventes_hl=("Volume vendu (hl)", "sum"),
        stock_hl=("Volume disponible (hl)", "sum"),
    )

    # Calcul du manque au niveau "reference" (gout + format)
    df_tmp = df.copy()
    df_tmp["vitesse_j_ligne"] = df_tmp["Volume vendu (hl)"] / jours
    df_tmp["demande_7j_ligne"] = 7.0 * df_tmp["vitesse_j_ligne"]
    df_tmp["stock_hl_ligne"] = df_tmp["Volume disponible (hl)"]
    df_tmp["manque_7j_ligne"] = np.clip(
        df_tmp["demande_7j_ligne"] - df_tmp["stock_hl_ligne"],
        a_min=0.0,
        a_max=None,
    )

    manque_par_gout = df_tmp.groupby("GoutCanon")["manque_7j_ligne"].sum()
    agg["manque_7j"] = manque_par_gout.reindex(agg.index, fill_value=0.0)

    # --- Selection : rupture -> perte euro -> autonomie ---
    agg["vitesse_j"] = agg["ventes_hl"] / jours
    agg["autonomie_j"] = np.where(
        agg["vitesse_j"] > 0,
        agg["stock_hl"] / agg["vitesse_j"],
        np.inf,
    )

    agg["rupture_semaine"] = agg["manque_7j"] > 1e-9
    agg["perte_7j"] = agg["manque_7j"] * PRICE_REF_HL

    agg = agg.sort_values(
        by=["rupture_semaine", "perte_7j", "autonomie_j"],
        ascending=[False, False, True],
    )

    if not manual_keep:
        g_rupt = [g for g, r in zip(agg.index.tolist(), agg["rupture_semaine"].tolist()) if r]
        g_other = [g for g, r in zip(agg.index.tolist(), agg["rupture_semaine"].tolist()) if not r]
        gouts_cibles = (g_rupt + g_other)[:nb_gouts]
    else:
        gouts_cibles = sorted(set(df["GoutCanon"]))
        if len(gouts_cibles) > nb_gouts:
            order = [g for g in agg.index if g in gouts_cibles]
            gouts_cibles = order[:nb_gouts]

    # --- Contrainte dure : si 2 gouts -> meme categorie ---
    if nb_gouts == 2 and len(gouts_cibles) == 2:
        def _rank_candidates_for_category(cat: str) -> list[str]:
            pool = [g for g in agg.index if _category(g) == cat]
            if not pool:
                return []
            sub = agg.loc[pool, ["rupture_semaine", "perte_7j", "autonomie_j"]].copy()
            sub["__key__"] = list(sub.index)
            sub = sub.sort_values(
                by=["rupture_semaine", "perte_7j", "autonomie_j"],
                ascending=[False, False, True],
            )
            return sub["__key__"].tolist()

        cats = ["infusion", "kefir"]
        ranked_by_cat = {c: _rank_candidates_for_category(c) for c in cats}
        valid = [c for c in cats if len(ranked_by_cat[c]) >= 2]

        if valid:
            if len(valid) == 1:
                choose = valid[0]
            else:
                order_global = list(agg.index)
                pos = {c: order_global.index(ranked_by_cat[c][0]) for c in valid}
                choose = min(valid, key=lambda c: pos[c])

            new_pair = ranked_by_cat[choose][:2]
            if set(new_pair) != set(gouts_cibles):
                note_msg = (
                    "\u26a0\ufe0f Contrainte appliquée : pas de co-production **Infusion + Kéfir**. "
                    f"Sélection ajustée \u2192 deux recettes **{'Infusion' if choose == 'infusion' else 'Kéfir'}** "
                    f"({new_pair[0]} ; {new_pair[1]})."
                )
            gouts_cibles = new_pair

    df_selected = df[df["GoutCanon"].isin(gouts_cibles)].copy()
    if len(gouts_cibles) == 0:
        raise ValueError("Aucun goût sélectionné.")

    # ---- ALLOCATION PAR GOUT ----
    df_calc = df_selected.copy()
    df_calc["v_i"] = df_calc["Volume vendu (hl)"] / max(float(window_days), 1.0)
    df_calc["G_i (hL)"] = df_calc["Volume disponible (hl)"]
    V_tot = float(volume_cible)

    # Partage du volume entre gouts
    ventes_par_gout = df_calc.groupby("GoutCanon")["Volume vendu (hl)"].sum()
    pos = ventes_par_gout > 0
    if repartir_pro_rv and pos.any():
        w_gout = (ventes_par_gout[pos] / ventes_par_gout[pos].sum()).reindex(
            ventes_par_gout.index, fill_value=0.0
        )
    else:
        n = max(len(ventes_par_gout), 1)
        w_gout = pd.Series(1.0 / n, index=ventes_par_gout.index)

    df_calc["X_adj (hL)"] = 0.0
    for g, grp in df_calc.groupby("GoutCanon"):
        Vg = V_tot * float(w_gout.get(g, 0.0))
        Gi = grp["G_i (hL)"].to_numpy(float)
        vi = np.maximum(grp["v_i"].to_numpy(float), 0.0)
        xg = _equalize_last_batch_global(Gi, vi, Vg)
        df_calc.loc[grp.index, "X_adj (hL)"] = np.maximum(xg, 0.0)

    cap_resume = f"{volume_cible:.2f} hL au total (égalité du jour d'épuisement par goût)"

    # ---- conversions cartons / bouteilles ----
    df_calc["Cartons à produire (exact)"] = df_calc["X_adj (hL)"] / df_calc["Volume/carton (hL)"]
    if ROUND_TO_CARTON:
        df_calc["Cartons à produire (arrondi)"] = np.floor(
            df_calc["Cartons à produire (exact)"] + 0.5
        ).astype("Int64")
        df_calc["Volume produit arrondi (hL)"] = (
            df_calc["Cartons à produire (arrondi)"] * df_calc["Volume/carton (hL)"]
        )

    df_calc["Bouteilles à produire (exact)"] = (
        df_calc["Cartons à produire (exact)"] * df_calc["Bouteilles/carton"]
    )
    if ROUND_TO_CARTON:
        df_calc["Bouteilles à produire (arrondi)"] = (
            df_calc["Cartons à produire (arrondi)"] * df_calc["Bouteilles/carton"]
        ).astype("Int64")

    df_min = (
        df_calc[
            [
                "GoutCanon", "Produit", "Stock",
                "Cartons à produire (arrondi)",
                "Bouteilles à produire (arrondi)",
                "Volume produit arrondi (hL)",
            ]
        ]
        .sort_values(["GoutCanon", "Produit", "Stock"])
        .reset_index(drop=True)
    )

    # ---- synthese selection ----
    agg_full = df.groupby("GoutCanon").agg(
        ventes_hl=("Volume vendu (hl)", "sum"),
        stock_hl=("Volume disponible (hl)", "sum"),
    )
    agg_full["vitesse_j"] = agg_full["ventes_hl"] / max(float(window_days), 1.0)
    agg_full["jours_autonomie"] = np.where(
        agg_full["vitesse_j"] > 0, agg_full["stock_hl"] / agg_full["vitesse_j"], np.inf
    )
    agg_full["score_urgence"] = agg_full["vitesse_j"] / (agg_full["jours_autonomie"] + EPS)
    sel_gouts = sorted(set(df_calc["GoutCanon"]))
    synth_sel = agg_full.loc[sel_gouts][
        ["ventes_hl", "stock_hl", "vitesse_j", "jours_autonomie", "score_urgence"]
    ].copy()
    synth_sel = synth_sel.rename(
        columns={
            "ventes_hl": "Ventes 2 mois (hL)",
            "stock_hl": "Stock (hL)",
            "vitesse_j": "Vitesse (hL/j)",
            "jours_autonomie": "Autonomie (jours)",
            "score_urgence": "Score urgence",
        }
    )

    # 7 sorties (comme utilise par la page Production)
    return df_min, cap_resume, sel_gouts, synth_sel, df_calc, df, note_msg
