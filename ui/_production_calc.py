"""
ui/_production_calc.py
======================
Fonctions de calcul de production (pas d'UI).

Extrait de ui/production.py pour maintenabilité.
"""
from __future__ import annotations

import logging
import time as _time

import pandas as pd

from core.optimizer import compute_plan

_log = logging.getLogger("ferment.production")

# ─── Cache produits EasyBeer ─────────────────────────────────────────────────

_EB_PRODUCTS_CACHE: dict = {"data": None, "ts": 0.0}
_EB_PRODUCTS_TTL = 3600  # 60 minutes — les produits changent rarement


def invalidate_eb_products_cache() -> None:
    """Invalide le cache produits (appelé manuellement via bouton « Rafraîchir »)."""
    _EB_PRODUCTS_CACHE["data"] = None
    _EB_PRODUCTS_CACHE["ts"] = 0.0


def _fetch_eb_products() -> list[dict]:
    """Produits EasyBeer avec cache TTL 60 min (évite les appels HTTP redondants).

    Ne met PAS en cache les échecs (exception ou liste vide) pour que le
    prochain appel retente l'API au lieu de servir un résultat vide.
    """
    now = _time.monotonic()
    if _EB_PRODUCTS_CACHE["data"] is not None and (now - _EB_PRODUCTS_CACHE["ts"]) < _EB_PRODUCTS_TTL:
        return _EB_PRODUCTS_CACHE["data"]
    from common.easybeer import get_all_products
    data = get_all_products()
    if data:  # ne cache que les résultats non-vides
        _EB_PRODUCTS_CACHE["data"] = data
        _EB_PRODUCTS_CACHE["ts"] = now
    return data


def _auto_match(gout: str, prod_labels: list[str]) -> int:
    """Retourne l'index du produit EasyBeer dont le libellé contient le goût."""
    g_low = gout.lower()
    for i, lbl in enumerate(prod_labels):
        if g_low in lbl.lower():
            return i
    return 0


# ─── Construction tableau final ──────────────────────────────────────────────

def _build_final_table(
    df_all: pd.DataFrame,
    df_calc: pd.DataFrame,
    gouts_cibles: list[str],
    overrides: dict,
) -> pd.DataFrame:
    """Construit le tableau avec tous les formats + overrides + redistribution."""
    sel = set(gouts_cibles)
    base = (
        df_all[df_all["GoutCanon"].isin(sel)][
            ["GoutCanon", "Produit", "Stock", "Volume/carton (hL)", "Bouteilles/carton"]
        ]
        .drop_duplicates(subset=["GoutCanon", "Produit", "Stock"])
        .copy()
        .reset_index(drop=True)
    )
    base = base.merge(
        df_calc[["GoutCanon", "Produit", "Stock", "X_adj (hL)"]],
        on=["GoutCanon", "Produit", "Stock"],
        how="left",
    )
    base["X_adj (hL)"] = base["X_adj (hL)"].fillna(0.0)

    rows_out = []
    for g, grp in base.groupby("GoutCanon", sort=False):
        V_g = grp["X_adj (hL)"].sum()
        forced_vol_g = 0.0
        for _, row in grp.iterrows():
            key = f"{row['GoutCanon']}|{row['Produit']}|{row['Stock']}"
            if key in overrides:
                forced_vol_g += overrides[key] * row["Volume/carton (hL)"]
        remaining_g = max(0.0, V_g - forced_vol_g)
        nf_weight = grp.loc[
            grp.apply(
                lambda r: f"{r['GoutCanon']}|{r['Produit']}|{r['Stock']}" not in overrides,
                axis=1,
            ),
            "X_adj (hL)",
        ].sum()

        for _, row in grp.iterrows():
            key = f"{row['GoutCanon']}|{row['Produit']}|{row['Stock']}"
            forced = overrides.get(key)
            if forced is not None:
                cartons = max(0, int(forced))
            else:
                if nf_weight > 1e-9 and row["X_adj (hL)"] > 0:
                    alloc_hl = remaining_g * row["X_adj (hL)"] / nf_weight
                    cartons = max(0, int(round(alloc_hl / row["Volume/carton (hL)"])))
                else:
                    cartons = 0
            bouteilles = int(cartons * row["Bouteilles/carton"])
            vol = round(cartons * row["Volume/carton (hL)"], 3)
            rows_out.append({
                "GoutCanon": row["GoutCanon"],
                "Produit": row["Produit"],
                "Stock": row["Stock"],
                "Volume/carton (hL)": row["Volume/carton (hL)"],
                "Bouteilles/carton": int(row["Bouteilles/carton"]),
                "Cartons à produire (arrondi)": cartons,
                "Bouteilles à produire (arrondi)": bouteilles,
                "Volume produit arrondi (hL)": vol,
                "_forcé": forced is not None,
            })
    return pd.DataFrame(rows_out) if rows_out else pd.DataFrame()


# ─── Calcul lourd (exécuté dans le thread pool) ─────────────────────────────

def _compute_production_sync(
    df_in_filtered: pd.DataFrame,
    window_days: int,
    volume_cible: float,
    effective_nb_gouts: int,
    repartir_pro_rv: bool,
    forced_gouts: list[str],
    excluded_gouts: list[str],
    mode_prod: str,
    overrides: dict,
    *,
    TANK_CONFIGS: dict,
    DEFAULT_LOSS_LARGE: int,
    DEFAULT_LOSS_SMALL: int,
) -> dict:
    """Passe 1 (optimiseur) + Passe 2 (EasyBeer) — aucun appel UI."""
    # ── PASSE 1 : Optimiseur
    (
        df_min, cap_resume, gouts_cibles, synth_sel,
        df_calc, df_all, note_msg,
    ) = compute_plan(
        df_in=df_in_filtered,
        window_days=window_days,
        volume_cible=volume_cible,
        nb_gouts=effective_nb_gouts,
        repartir_pro_rv=repartir_pro_rv,
        manual_keep=forced_gouts or None,
        exclude_list=excluded_gouts,
    )

    # ── PASSE 2 : Aromatisation (modes auto)
    volume_details: dict = {}
    if mode_prod != "Manuel" and gouts_cibles:
        from common.easybeer import (
            compute_aromatisation_volume,
            compute_dilution_ingredients,
            compute_v_start_max,
        )
        from common.easybeer import (
            is_configured as _eb_conf_p2,
        )
        _tank_cfg = TANK_CONFIGS[mode_prod]
        _C = _tank_cfg["capacity"]
        _Lt = _tank_cfg["transfer_loss"]
        _Lb = _tank_cfg["bottling_loss"]
        _gout_p2 = gouts_cibles[0]
        _A_R, _R = 0.0, 0.0
        _id_prod_p2 = None

        if _eb_conf_p2():
            try:
                _eb_prods_p2 = _fetch_eb_products()
                _labels_p2 = [p.get("libelle", "") for p in _eb_prods_p2]
                _matched_idx = _auto_match(_gout_p2, _labels_p2)
                _id_prod_p2 = _eb_prods_p2[_matched_idx]["idProduit"]
                _A_R, _R = compute_aromatisation_volume(_id_prod_p2)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                _log.warning("Erreur calcul aromatisation pour %s: %s", _gout_p2, exc, exc_info=True)
                _A_R, _R = 0.0, 0.0

        _V_start, _V_bottled = compute_v_start_max(_C, _Lt, _Lb, _A_R, _R)
        _volume_cible_recalc = _V_bottled / 100.0

        _is_infusion_p2 = False
        _dilution_p2: dict = {}
        if _id_prod_p2 is not None:
            try:
                _prod_label_p2 = _eb_prods_p2[_matched_idx].get("libelle", "")
                _is_infusion_p2 = (
                    "infusion" in _prod_label_p2.lower()
                    or _prod_label_p2.upper().startswith("EP")
                )
            except (IndexError, KeyError, AttributeError):
                _log.debug("Erreur détection infusion pour %s", _gout_p2, exc_info=True)
            try:
                _dilution_p2 = compute_dilution_ingredients(_id_prod_p2, _V_start)
            except (ValueError, TypeError, KeyError) as exc:
                _log.warning("Erreur calcul dilution p2 pour %s: %s", _gout_p2, exc, exc_info=True)
                _dilution_p2 = {}

        volume_details[_gout_p2] = {
            "V_start": _V_start,
            "A_R": _A_R,
            "R": _R,
            "V_aroma": _A_R * (_V_start / _R) if _R > 0 else 0.0,
            "V_bottled": _V_bottled,
            "capacity": _C,
            "transfer_loss": _Lt,
            "bottling_loss": _Lb,
            "is_infusion": _is_infusion_p2,
            "dilution_ingredients": _dilution_p2,
            "id_produit": _id_prod_p2,
        }

        # Relance si le volume a changé
        if abs(_volume_cible_recalc - volume_cible) > 0.01:
            volume_cible = _volume_cible_recalc
            try:
                (
                    df_min, cap_resume, gouts_cibles, synth_sel,
                    df_calc, df_all, note_msg,
                ) = compute_plan(
                    df_in=df_in_filtered,
                    window_days=window_days,
                    volume_cible=volume_cible,
                    nb_gouts=effective_nb_gouts,
                    repartir_pro_rv=repartir_pro_rv,
                    manual_keep=forced_gouts or None,
                    exclude_list=excluded_gouts,
                )
            except (ValueError, KeyError, pd.errors.MergeError) as exc:
                _log.exception("Erreur compute_plan: %s", exc)

    df_final = _build_final_table(df_all, df_calc, gouts_cibles, overrides)

    return {
        "df_min": df_min,
        "cap_resume": cap_resume,
        "gouts_cibles": gouts_cibles,
        "synth_sel": synth_sel,
        "df_calc": df_calc,
        "df_all": df_all,
        "note_msg": note_msg,
        "volume_details": volume_details,
        "volume_cible": volume_cible,
        "df_final": df_final,
    }
