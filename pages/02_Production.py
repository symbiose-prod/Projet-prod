from __future__ import annotations
from common.session import require_login, user_menu, user_menu_footer
user = require_login()  # stoppe la page si non connecté
user_menu()             # affiche l'info utilisateur + bouton logout dans la sidebar

import os
import re
import datetime as _dt
import pandas as pd
import streamlit as st
from dateutil.relativedelta import relativedelta

from common.design import apply_theme, section, kpi, find_image_path, load_image_bytes
from common.data import get_paths
from core.optimizer import (
    load_flavor_map_from_path,
    apply_canonical_flavor, sanitize_gouts,
    compute_plan,
)
from common.xlsx_fill import fill_fiche_xlsx

# ====== Template unique fiche de production ======
TEMPLATE_PATH = "assets/Fiche_production.xlsx"

# ====== Configurations cuves ======
TANK_CONFIGS = {
    "Cuve de 7200L (1 goût)": {"capacity": 7200, "transfer_loss": 400, "bottling_loss": 400, "nb_gouts": 1, "nominal_hL": 64.0},
    "Cuve de 5200L (1 goût)": {"capacity": 5200, "transfer_loss": 200, "bottling_loss": 200, "nb_gouts": 1, "nominal_hL": 48.0},
    "Manuel": None,
}

# ====== Cache produits EasyBeer (utilisé dans passe 2 + section création) ======
@st.cache_data(ttl=300, show_spinner="Chargement des produits EasyBeer…")
def _fetch_eb_products():
    from common.easybeer import get_all_products
    return get_all_products()

# ---------------- UI header ----------------
apply_theme("Production — Ferment Station", "📦")
section("Tableau de production", "📦")

# ---------------- Pré-requis : fichier chargé sur Accueil ----------------
if "df_raw" not in st.session_state or "window_days" not in st.session_state:
    st.warning("Aucun fichier chargé. Va dans **Accueil** pour déposer l'Excel, puis reviens.")
    st.stop()

# chemins (repo)
_, flavor_map, images_dir = get_paths()

# Données depuis l'accueil
df_in_raw = st.session_state.df_raw
window_days = st.session_state.window_days

# ---------------- Préparation des données ----------------
fm = load_flavor_map_from_path(flavor_map)
try:
    df_in = apply_canonical_flavor(df_in_raw, fm)
except KeyError as e:
    st.error(f"{e}")
    st.info("Astuce : vérifie la 1ère ligne (en-têtes) de ton Excel et renomme la colonne du nom produit en **'Produit'** ou **'Désignation'**.")
    st.stop()

df_in["Produit"] = df_in["Produit"].astype(str)
df_in = sanitize_gouts(df_in)

# ---------------- Sidebar (paramètres) ----------------
with st.sidebar:
    st.header("Paramètres")
    mode_prod = st.radio(
        "Mode de production",
        options=list(TANK_CONFIGS.keys()),
        index=0,
        help=(
            "**Cuve de 7200L / 5200L** : volume calculé automatiquement "
            "en tenant compte des ingrédients d'aromatisation (jus, arômes). "
            "**Manuel** : choisis toi-même le volume cible et le nombre de goûts."
        ),
    )

    if mode_prod == "Manuel":
        volume_cible = st.number_input("Volume cible (hL)", 1.0, 1000.0, 64.0, 1.0)
        nb_gouts = st.selectbox("Nombre de goûts simultanés", [1, 2], index=0)
    else:
        _tank = TANK_CONFIGS[mode_prod]
        nb_gouts = _tank["nb_gouts"]
        volume_cible = _tank["nominal_hL"]  # nominal pour Passe 1

    repartir_pro_rv = st.checkbox("Répartition au prorata des ventes", value=True)

    st.markdown("---")
    st.subheader("Filtres")
    all_gouts = sorted(pd.Series(df_in.get("GoutCanon", pd.Series(dtype=str))).dropna().astype(str).str.strip().unique())
    excluded_gouts = st.multiselect("🚫 Exclure certains goûts", options=all_gouts, default=[])
    
    # 🔥 NOUVEAU : exclusion précise par produit (Produit + Stock)
    # On la place juste sous "Exclure certains goûts"
    try:
        df_preview = df_in.copy()
        # Clef lisible combinant Goût, Produit et Stock
        df_preview["Produit complet"] = df_preview.apply(
            lambda r: f"{r.get('Produit','').strip()} — {r.get('Stock','').strip()}"
            if pd.notna(r.get('Stock')) else r.get('Produit','').strip(),
            axis=1
        )
    
        product_options = sorted(df_preview["Produit complet"].dropna().unique().tolist())
    except Exception:
        product_options = []
    
    excluded_products = st.multiselect(
        "🚫 Exclure certains produits (Produit + Stock)",
        options=product_options,
        default=[],
        help="Exclut les produits précis (ex : Kéfir Gingembre — Carton de 12 Bouteilles – 0,33 L)"
    )

    # 🔥 NOUVEAU : forcer certains goûts
    forced_gouts = st.multiselect(
        "✅ Forcer la production de ces goûts",
        options=[g for g in all_gouts if g not in set(excluded_gouts)],
        help="Les goûts sélectionnés ici seront produits quoi qu'il arrive. "
             "Si tu en choisis plus que le nombre de goûts sélectionnés ci-dessus, "
             "le nombre sera automatiquement augmenté."
    )

    st.markdown("---")
    user_menu_footer(user)
    
st.caption(
    f"Fichier courant : **{st.session_state.get('file_name','(sans nom)')}** — Fenêtre (B2) : **{window_days} jours**"
)

# ---------------- Filtrage des produits exclus (en amont du calcul) ----------------
if excluded_products:
    mask_excl_input = df_in.apply(
        lambda r: f"{r.get('Produit','').strip()} — {r.get('Stock','').strip()}" in excluded_products,
        axis=1
    )
    df_in_filtered = df_in.loc[~mask_excl_input].copy()
else:
    df_in_filtered = df_in.copy()

# ---------------- Calculs ----------------
# Nombre de goûts effectif : on garantit que tous les 'forcés' rentrent
effective_nb_gouts = max(nb_gouts, len(forced_gouts)) if forced_gouts else nb_gouts

(
    df_min,
    cap_resume,
    gouts_cibles,
    synth_sel,
    df_calc,
    df_all,
    note_msg,
) = compute_plan(
    df_in=df_in_filtered,              # <<< on relance avec df_in filtré
    window_days=window_days,
    volume_cible=volume_cible,
    nb_gouts=effective_nb_gouts,
    repartir_pro_rv=repartir_pro_rv,
    manual_keep=forced_gouts or None,
    exclude_list=excluded_gouts,
)

# ── PASSE 2 (modes auto) : recalcul du volume avec aromatisation ─────────
_volume_details: dict = {}  # {gout: {V_start, A_R, R, V_aroma, V_bottled, ...}}

if mode_prod != "Manuel" and gouts_cibles:
    from common.easybeer import (
        is_configured as _eb_conf_p2,
        compute_aromatisation_volume,
        compute_v_start_max,
        compute_dilution_ingredients,
    )

    _tank_cfg = TANK_CONFIGS[mode_prod]
    _C = _tank_cfg["capacity"]
    _Lt = _tank_cfg["transfer_loss"]
    _Lb = _tank_cfg["bottling_loss"]

    _gout_p2 = gouts_cibles[0]  # mode auto = 1 goût
    _A_R, _R = 0.0, 0.0

    if _eb_conf_p2():
        try:
            _eb_prods_p2 = _fetch_eb_products()
            _labels_p2 = [p.get("libelle", "") for p in _eb_prods_p2]
            _g_low = _gout_p2.lower()
            _matched_idx = next(
                (i for i, lbl in enumerate(_labels_p2) if _g_low in lbl.lower()), 0
            )
            _id_prod_p2 = _eb_prods_p2[_matched_idx]["idProduit"]
            _A_R, _R = compute_aromatisation_volume(_id_prod_p2)
        except Exception:
            _A_R, _R = 0.0, 0.0

    _V_start, _V_bottled = compute_v_start_max(_C, _Lt, _Lb, _A_R, _R)
    _volume_cible_recalc = _V_bottled / 100.0

    # Detect infusion vs kefir + fetch dilution ingredients
    _is_infusion_p2 = False
    _dilution_p2: dict[str, float] = {}
    _id_prod_p2_safe = locals().get("_id_prod_p2")

    if _id_prod_p2_safe is not None:
        try:
            _prod_label_p2 = _eb_prods_p2[_matched_idx].get("libelle", "")
            _is_infusion_p2 = "infusion" in _prod_label_p2.lower() or _prod_label_p2.upper().startswith("EP")
        except Exception:
            pass

        try:
            _dilution_p2 = compute_dilution_ingredients(_id_prod_p2_safe, _V_start)
        except Exception:
            _dilution_p2 = {}

    _volume_details[_gout_p2] = {
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

    # Relance l'optimiseur si le volume a changé significativement
    if abs(_volume_cible_recalc - volume_cible) > 0.01:
        volume_cible = _volume_cible_recalc
        (
            df_min, cap_resume, gouts_cibles, synth_sel, df_calc, df_all, note_msg,
        ) = compute_plan(
            df_in=df_in_filtered,
            window_days=window_days,
            volume_cible=volume_cible,
            nb_gouts=effective_nb_gouts,
            repartir_pro_rv=repartir_pro_rv,
            manual_keep=forced_gouts or None,
            exclude_list=excluded_gouts,
        )

# ✅ Affiche la note d'ajustement (ex: contrainte Infusion/Kéfir)
if isinstance(note_msg, str) and note_msg.strip():
    st.info(note_msg)

# ── Détails du calcul de volume (modes auto) ──
if _volume_details:
    for _g_vd, _vd in _volume_details.items():
        with st.expander(f"📐 Détails du calcul de volume — {_g_vd}", expanded=False):
            _c1v, _c2v, _c3v, _c4v = st.columns(4)
            with _c1v: kpi("V d\u00e9part (L)", f"{_vd['V_start']:.0f}")
            with _c2v: kpi("Aromatisation (L)", f"{_vd['V_aroma']:.0f}")
            with _c3v: kpi("V embouteill\u00e9 (L)", f"{_vd['V_bottled']:.0f}")
            with _c4v: kpi("Volume cible (hL)", f"{_vd['V_bottled']/100:.2f}")
            st.caption(
                f"Cuve {_vd['capacity']}L \u2014 "
                f"Perte transfert : {_vd['transfer_loss']}L \u2014 "
                f"Perte embouteillage : {_vd['bottling_loss']}L \u2014 "
                f"Recette : {_vd['R']:.0f}L (r\u00e9f) avec {_vd['A_R']:.1f}L d'aromatisation"
            )

# ─── Overrides manuels (clé=(GoutCanon,Produit,Stock), valeur=nb cartons forcés) ─
if "manual_overrides" not in st.session_state:
    st.session_state.manual_overrides = {}

# ─── Fonctions utilitaires ──────────────────────────────────────────────────────
def sku_guess(name: str):
    m = re.search(r"\b([A-Z]{3,6}-\d{2,3})\b", str(name))
    return m.group(1) if m else None

def _build_final_table(df_all, df_calc, gouts_cibles, overrides):
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
    # Merge X_adj de l'optimiseur (poids de redistribution)
    base = base.merge(
        df_calc[["GoutCanon", "Produit", "Stock", "X_adj (hL)"]],
        on=["GoutCanon", "Produit", "Stock"],
        how="left",
    )
    base["X_adj (hL)"] = base["X_adj (hL)"].fillna(0.0)

    rows_out = []
    for g, grp in base.groupby("GoutCanon", sort=False):
        V_g = grp["X_adj (hL)"].sum()
        # Volume consommé par les lignes forcées de ce goût
        forced_vol_g = 0.0
        for _, row in grp.iterrows():
            key = (row["GoutCanon"], row["Produit"], row["Stock"])
            if key in overrides:
                forced_vol_g += overrides[key] * row["Volume/carton (hL)"]
        remaining_g = max(0.0, V_g - forced_vol_g)
        # Poids des lignes non-forcées (basé sur X_adj de l'optimiseur)
        nf_weight = grp.loc[
            grp.apply(lambda r: (r["GoutCanon"], r["Produit"], r["Stock"]) not in overrides, axis=1),
            "X_adj (hL)",
        ].sum()

        for _, row in grp.iterrows():
            key = (row["GoutCanon"], row["Produit"], row["Stock"])
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

df_final = _build_final_table(
    df_all, df_calc, gouts_cibles, st.session_state.manual_overrides
)

# ─── KPIs ──────────────────────────────────────────────────────────────────────
total_btl = int(df_final["Bouteilles à produire (arrondi)"].sum()) if not df_final.empty else 0
total_vol = float(df_final["Volume produit arrondi (hL)"].sum()) if not df_final.empty else 0.0
nb_actifs = int((df_final["Cartons à produire (arrondi)"] > 0).sum()) if not df_final.empty else 0
nb_forcés = int(df_final["_forcé"].sum()) if not df_final.empty else 0
c1, c2, c3 = st.columns(3)
with c1: kpi("Total bouteilles à produire", f"{total_btl:,}".replace(",", " "))
with c2: kpi("Volume total (hL)", f"{total_vol:.2f}")
with c3: kpi("Formats en production", f"{nb_actifs}" + (f" ({nb_forcés} forcé{'s' if nb_forcés>1 else ''})" if nb_forcés else ""))

# ─── Tableau éditable (tous les formats) ───────────────────────────────────────
_col_info, _col_reset = st.columns([5, 1])
with _col_info:
    if nb_forcés:
        st.caption(f"✏️ **{nb_forcés} ligne(s) forcée(s)** — le volume restant est redistribué proportionnellement.")
    else:
        st.caption("Colonne **✏️ Forcer** : tape un nombre de cartons pour forcer une ligne. Laisse vide = valeur automatique.")
with _col_reset:
    if st.button("♻️ Réinitialiser", key="btn_reset_overrides", use_container_width=True,
                 help="Annule tous les forcés manuels et revient aux valeurs de l'optimiseur."):
        st.session_state.manual_overrides = {}
        st.rerun()

if not df_final.empty:
    df_view = df_final.copy()
    df_view["__img_path"] = [
        find_image_path(images_dir, sku=sku_guess(p), flavor=g)
        for p, g in zip(df_view["Produit"], df_view["GoutCanon"])
    ]
    df_view["Image"] = df_view["__img_path"].apply(load_image_bytes)

    # Colonne "Forcer" : nullable int (pd.NA = vide = auto)
    _ov = st.session_state.manual_overrides
    df_view["✏️ Forcer"] = pd.array(
        [_ov.get((r["GoutCanon"], r["Produit"], r["Stock"])) for _, r in df_view.iterrows()],
        dtype=pd.Int64Dtype(),
    )

    edited = st.data_editor(
        df_view[[
            "Image", "GoutCanon", "Produit", "Stock",
            "✏️ Forcer",
            "Cartons à produire (arrondi)", "Bouteilles à produire (arrondi)",
            "Volume produit arrondi (hL)",
        ]],
        use_container_width=True,
        hide_index=True,
        disabled=[
            "Image", "GoutCanon", "Produit", "Stock",
            "Cartons à produire (arrondi)", "Bouteilles à produire (arrondi)",
            "Volume produit arrondi (hL)",
        ],
        column_config={
            "Image": st.column_config.ImageColumn("Image", width="small"),
            "GoutCanon": "Goût",
            "✏️ Forcer": st.column_config.NumberColumn(
                "✏️ Forcer (cartons)",
                min_value=0,
                step=1,
                help="Force le nombre de cartons pour cette ligne. Laisse vide pour la valeur automatique.",
            ),
            "Cartons à produire (arrondi)": st.column_config.NumberColumn("Cartons"),
            "Bouteilles à produire (arrondi)": st.column_config.NumberColumn("Bouteilles"),
            "Volume produit arrondi (hL)": st.column_config.NumberColumn("Volume (hL)", format="%.3f"),
        },
        key="table_prod_edit",
    )

    # Capture des overrides depuis la table éditée → mise à jour session state
    new_overrides = {}
    for _, row in edited.iterrows():
        v = row.get("✏️ Forcer")
        try:
            if not pd.isna(v):
                vi = int(v)
                if vi >= 0:
                    new_overrides[(row["GoutCanon"], row["Produit"], row["Stock"])] = vi
        except (TypeError, ValueError):
            pass
    if new_overrides != st.session_state.manual_overrides:
        st.session_state.manual_overrides = new_overrides
        st.rerun()
else:
    st.warning("Aucun format disponible pour les goûts sélectionnés.")

# df_min compatible avec le reste du code (save, Excel…) — ne garde que les >0 cartons
df_min_override = (
    df_final[df_final["Cartons à produire (arrondi)"] > 0][[
        "GoutCanon", "Produit", "Stock",
        "Cartons à produire (arrondi)",
        "Bouteilles à produire (arrondi)",
        "Volume produit arrondi (hL)",
    ]].copy().reset_index(drop=True)
    if not df_final.empty else df_min.copy()
)

# ======================================================================
# ========== Sauvegarde + génération de la fiche Excel ==================
# ======================================================================
section("Fiche de production (modèle Excel)", "🧾")

_sp_prev = st.session_state.get("saved_production")
default_debut = _dt.date.fromisoformat(_sp_prev["semaine_du"]) if _sp_prev and "semaine_du" in _sp_prev else _dt.date.today()

# Champ unique : date de début fermentation
date_debut = st.date_input("Date de début de fermentation", value=default_debut)

# DDM = début + 1 an
date_ddm = date_debut + _dt.timedelta(days=365)

if st.button("💾 Sauvegarder cette production", use_container_width=True):
    g_order = []
    if isinstance(df_min_override, pd.DataFrame) and "GoutCanon" in df_min_override.columns:
        for g in df_min_override["GoutCanon"].astype(str).tolist():
            if g and g not in g_order:
                g_order.append(g)

    st.session_state.saved_production = {
        "df_min": df_min_override.copy(),
        "df_calc": df_calc.copy(),
        "gouts": g_order,
        "semaine_du": date_debut.isoformat(),
        "ddm": date_ddm.isoformat(),
        "volume_details": dict(_volume_details),
        "mode_prod": mode_prod,
    }
    st.success("Production sauvegardée ✅ — tu peux maintenant générer la fiche.")


sp = st.session_state.get("saved_production")

def _two_gouts_auto(sp_obj, df_min_cur, gouts_cur):
    if isinstance(sp_obj, dict):
        g_saved = sp_obj.get("gouts")
        if g_saved:
            uniq = []
            for g in g_saved:
                if g and g not in uniq:
                    uniq.append(g)
            if uniq:
                return (uniq + [None, None])[:2]
    if isinstance(df_min_cur, pd.DataFrame) and "GoutCanon" in df_min_cur.columns:
        seen = []
        for g in df_min_cur["GoutCanon"].astype(str).tolist():
            if g and g not in seen:
                seen.append(g)
        if seen:
            return (seen + [None, None])[:2]
    base = list(gouts_cur) if gouts_cur else []
    return (base + [None, None])[:2]

if sp:
    g1, g2 = _two_gouts_auto(sp, sp.get("df_min", df_min_override), gouts_cibles)

    _sp_vd = sp.get("volume_details") or {}
    _vd_dl = _sp_vd.get(g1, {})

    if not os.path.exists(TEMPLATE_PATH):
        st.error(f"Modele introuvable : **{TEMPLATE_PATH}**. Place le fichier dans le repo.")
    else:
        try:
            xlsx_bytes = fill_fiche_xlsx(
                template_path=TEMPLATE_PATH,
                semaine_du=_dt.date.fromisoformat(sp["semaine_du"]),
                ddm=_dt.date.fromisoformat(sp["ddm"]),
                gout1=g1 or "",
                gout2=g2,
                df_calc=sp.get("df_calc", df_calc),
                df_min=sp.get("df_min", df_min_override),
                V_start=_vd_dl.get("V_start", 0),
                tank_capacity=_vd_dl.get("capacity", 7200),
                transfer_loss=_vd_dl.get("transfer_loss", 400),
                aromatisation_volume=_vd_dl.get("V_aroma", 0),
                is_infusion=_vd_dl.get("is_infusion", False),
                dilution_ingredients=_vd_dl.get("dilution_ingredients"),
            )

            semaine_label = _dt.date.fromisoformat(sp["semaine_du"]).strftime("%d-%m-%Y")
            fname_xlsx = f"Fiche de production - {g1 or 'Multi'} - {semaine_label}.xlsx"

            st.download_button(
                "📄 Télécharger la fiche (XLSX)",
                data=xlsx_bytes,
                file_name=fname_xlsx,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        except FileNotFoundError:
            st.error("Modele introuvable. Verifie le chemin du fichier modele.")
        except Exception as e:
            st.error(f"Erreur lors du remplissage du modele : {e}")
else:
    st.info("Sauvegarde la production ci-dessus pour activer la génération de la fiche.")

# ================== Créer dans EasyBeer ==================
section("Créer dans EasyBeer", "🍺")

from common.easybeer import is_configured as _eb_configured

if not _eb_configured():
    st.warning("EasyBeer n'est pas configuré (variables EASYBEER_API_USER / EASYBEER_API_PASS manquantes).")
elif not st.session_state.get("saved_production"):
    st.info("Sauvegarde d'abord une production ci-dessus pour pouvoir créer les brassins dans EasyBeer.")
else:
    _sp_eb = st.session_state["saved_production"]
    _gouts_eb = _sp_eb.get("gouts", [])
    _df_calc_eb = _sp_eb.get("df_calc")
    _semaine_du_eb = _sp_eb.get("semaine_du", "")

    if not _gouts_eb:
        st.warning("Aucun goût dans la production sauvegardée.")
    else:
        # --- Volume par goût pour EasyBeer ---
        _vol_par_gout: dict[str, float] = {}
        _nb_gouts_eb = len(_gouts_eb)

        if mode_prod != "Manuel" and _volume_details:
            # Mode auto : V_start déjà calculé (tient compte de l'aromatisation)
            for g in _gouts_eb:
                if g in _volume_details:
                    _vol_par_gout[g] = _volume_details[g]["V_start"]
                else:
                    _tank_eb = TANK_CONFIGS.get(mode_prod) or TANK_CONFIGS["Cuve de 7200L (1 goût)"]
                    _vol_par_gout[g] = float(_tank_eb["capacity"])
            _perte_litres = TANK_CONFIGS[mode_prod]["transfer_loss"] + TANK_CONFIGS[mode_prod]["bottling_loss"]
        else:
            # Mode Manuel : comportement existant
            _perte_litres = 800 if volume_cible > 50 else 400
            if _nb_gouts_eb == 1:
                _vol_par_gout[_gouts_eb[0]] = volume_cible * 100 + _perte_litres
            else:
                # Plusieurs goûts : répartir au prorata de X_adj (optimiseur)
                _proportions: dict[str, float] = {}
                _total_x = 0.0
                if isinstance(_df_calc_eb, pd.DataFrame) and "GoutCanon" in _df_calc_eb.columns:
                    _vol_col = "X_adj (hL)" if "X_adj (hL)" in _df_calc_eb.columns else None
                    if _vol_col:
                        for g in _gouts_eb:
                            mask = _df_calc_eb["GoutCanon"].astype(str) == g
                            val = float(_df_calc_eb.loc[mask, _vol_col].sum())
                            _proportions[g] = val
                            _total_x += val

                if _total_x > 0:
                    for g in _gouts_eb:
                        part = (_proportions.get(g, 0) / _total_x) * volume_cible
                        _vol_par_gout[g] = part * 100 + _perte_litres
                else:
                    # Fallback : répartition égale
                    for g in _gouts_eb:
                        _vol_par_gout[g] = (volume_cible / _nb_gouts_eb) * 100 + _perte_litres

        try:
            _eb_products = _fetch_eb_products()
        except Exception as e:
            st.error(f"Erreur de connexion à EasyBeer : {e}")
            _eb_products = []

        if _eb_products:
            # --- Matching automatique par nom ---
            _prod_labels = [p.get("libelle", "") for p in _eb_products]

            def _auto_match(gout: str) -> int:
                """Retourne l'index du produit EasyBeer dont le libellé contient le goût."""
                g_low = gout.lower()
                for i, lbl in enumerate(_prod_labels):
                    if g_low in lbl.lower():
                        return i
                return 0  # premier produit par défaut

            # --- Récap + sélections ---
            st.markdown(f"**Date de début :** {_semaine_du_eb}")
            st.markdown(f"**Goûts :** {', '.join(_gouts_eb)}")
            if mode_prod != "Manuel" and _volume_details:
                st.caption(
                    "Volume = V départ (base kéfir, calculé automatiquement)"
                )
            else:
                st.caption(f"Volume = volume cartons + {_perte_litres} L de perte")

            # --- Date d'embouteillage ---
            _default_embout = _dt.date.fromisoformat(_semaine_du_eb) + _dt.timedelta(days=7)
            _date_embouteillage = st.date_input(
                "Date d'embouteillage prévue",
                value=_default_embout,
                key="eb_date_embouteillage",
            )

            _selected_products: dict[str, int] = {}  # gout → idProduit
            for g in _gouts_eb:
                vol_l = _vol_par_gout.get(g, 0)
                col_g, col_p = st.columns([1, 2])
                with col_g:
                    st.metric(g, f"{vol_l:.0f} L")
                with col_p:
                    idx = st.selectbox(
                        f"Produit EasyBeer pour « {g} »",
                        options=range(len(_prod_labels)),
                        format_func=lambda i: _prod_labels[i],
                        index=_auto_match(g),
                        key=f"eb_prod_{g}",
                    )
                    _selected_products[g] = _eb_products[idx]["idProduit"]

            # --- Sélection du matériel (cuves) ---
            from common.easybeer import get_all_materiels

            _materiels: list[dict] = []
            try:
                _materiels = get_all_materiels()
            except Exception as _me:
                st.warning(f"Impossible de charger le matériel EasyBeer : {_me}")

            # Cuve de fermentation : filtrer par volume correspondant au mode
            _tank_cap_eb = 0
            if mode_prod != "Manuel" and _volume_details:
                _vd_first = list(_volume_details.values())[0]
                _tank_cap_eb = _vd_first.get("capacity", 0)
            elif mode_prod != "Manuel":
                _tank_cfg_eb = TANK_CONFIGS.get(mode_prod) or {}
                _tank_cap_eb = _tank_cfg_eb.get("capacity", 0) if _tank_cfg_eb else 0

            _cuves_fermentation = [
                m for m in _materiels
                if m.get("type", {}).get("code") == "CUVE_FERMENTATION"
                and abs(m.get("volume", 0) - _tank_cap_eb) < 100
            ] if _tank_cap_eb > 0 else []

            _cuve_dilution = next(
                (m for m in _materiels if m.get("type", {}).get("code") == "CUVE_FABRICATION"),
                None,
            )

            _selected_cuve_a_id: int | None = None  # fermentation + aromatisation
            _selected_cuve_b_id: int | None = None  # transfert + garde

            if _cuves_fermentation:
                st.markdown("---")
                st.markdown("**Affectation des cuves**")

                _cuve_labels = [
                    f"{m.get('identifiant', '')} ({m.get('volume', 0):.0f}L) — {m.get('etatCourant', {}).get('libelle', '?')}"
                    for m in _cuves_fermentation
                ]

                _col_ca, _col_cb = st.columns(2)
                with _col_ca:
                    _idx_a = st.selectbox(
                        "Cuve de fermentation (Cuve A)",
                        options=range(len(_cuve_labels)),
                        format_func=lambda i: _cuve_labels[i],
                        index=0,
                        key="eb_cuve_a",
                        help="Utilisée pour : Fermentation + Aromatisation",
                    )
                    _selected_cuve_a_id = _cuves_fermentation[_idx_a].get("idMateriel")

                with _col_cb:
                    # Cuve B = l'autre cuve du meme volume (par defaut la suivante)
                    _default_b = 1 if len(_cuves_fermentation) > 1 else 0
                    if _default_b == _idx_a and len(_cuves_fermentation) > 1:
                        _default_b = 0 if _idx_a != 0 else 1
                    _idx_b = st.selectbox(
                        "Cuve de garde (Cuve B)",
                        options=range(len(_cuve_labels)),
                        format_func=lambda i: _cuve_labels[i],
                        index=_default_b,
                        key="eb_cuve_b",
                        help="Utilisée pour : Transfert + Garde",
                    )
                    _selected_cuve_b_id = _cuves_fermentation[_idx_b].get("idMateriel")

                if _selected_cuve_a_id == _selected_cuve_b_id:
                    st.warning("Cuve A et Cuve B sont identiques. Choisis deux cuves distinctes.")

            # --- Bouton de création ---
            _already_created = st.session_state.get("_eb_brassins_created", {})
            _creation_key = f"{_semaine_du_eb}_{'_'.join(_gouts_eb)}"

            if _creation_key in _already_created:
                ids = _already_created[_creation_key]
                st.success(f"Brassins déjà créés pour cette production (IDs : {', '.join(str(i) for i in ids)}).")
                if st.button("🔄 Recréer les brassins", key="eb_recreate"):
                    del st.session_state["_eb_brassins_created"][_creation_key]
                    st.rerun()
            else:
                if st.button("🍺 Créer les brassins dans EasyBeer", type="primary", use_container_width=True, key="eb_create"):
                    from common.easybeer import (
                        create_brassin, get_product_detail, get_warehouses,
                        get_planification_matrice, add_planification_conditionnement,
                        upload_fichier_brassin,
                    )

                    from core.optimizer import parse_stock as _parse_stock

                    # --- Entrepôt principal (pour la planification) ---
                    _id_entrepot = None
                    try:
                        _warehouses = get_warehouses()
                        for _w in _warehouses:
                            if _w.get("principal"):
                                _id_entrepot = _w.get("idEntrepot")
                                break
                        if not _id_entrepot and _warehouses:
                            _id_entrepot = _warehouses[0].get("idEntrepot")
                    except Exception:
                        pass

                    created_ids = []
                    errors = []
                    for g in _gouts_eb:
                        vol_l = _vol_par_gout.get(g, 0)
                        id_produit = _selected_products[g]

                        # Nom court : Kéfir → K + 2 lettres goût ; Infusion → IP + 1 lettre goût
                        _date_obj = _dt.date.fromisoformat(_semaine_du_eb)
                        _prod_label = next((p.get("libelle", "") for p in _eb_products if p.get("idProduit") == id_produit), "")
                        if "infusion" in _prod_label.lower():
                            _code = "IP" + g[:1].upper() + _date_obj.strftime("%d%m%Y")
                        else:
                            _code = "K" + g[:2].upper() + _date_obj.strftime("%d%m%Y")

                        # Récupérer la recette du produit pour les ingrédients et étapes
                        _ingredients = []
                        _planif_etapes = []
                        try:
                            prod_detail = get_product_detail(id_produit)
                            recettes = prod_detail.get("recettes") or []
                            etapes = prod_detail.get("etapes") or []

                            # Ingrédients : mise à l'échelle selon le volume du brassin
                            if recettes:
                                recette = recettes[0]
                                vol_recette = recette.get("volumeRecette", 0)
                                ratio = vol_l / vol_recette if vol_recette > 0 else 1
                                for ing in recette.get("ingredients") or []:
                                    _ingredients.append({
                                        "idProduitIngredient": ing.get("idProduitIngredient"),
                                        "matierePremiere": ing.get("matierePremiere"),
                                        "quantite": round(ing.get("quantite", 0) * ratio, 2),
                                        "ordre": ing.get("ordre", 0),
                                        "unite": ing.get("unite"),
                                        "brassageEtape": ing.get("brassageEtape"),
                                        "modeleNumerosLots": [],
                                    })

                            # Étapes de production (avec affectation matériel)
                            import unicodedata as _ud_etape

                            def _norm_etape(s: str) -> str:
                                s = _ud_etape.normalize("NFKD", s)
                                s = "".join(ch for ch in s if not _ud_etape.combining(ch))
                                return s.lower()

                            for et in etapes:
                                # Déterminer le matériel pour cette étape
                                _etape_nom = _norm_etape(
                                    (et.get("brassageEtape") or {}).get("nom", "")
                                )
                                _mat_for_step = {}
                                if _selected_cuve_a_id and ("fermentation" in _etape_nom or "aromatisation" in _etape_nom or "filtration" in _etape_nom):
                                    _mat_for_step = {"idMateriel": _selected_cuve_a_id}
                                elif _selected_cuve_b_id and ("transfert" in _etape_nom or "garde" in _etape_nom):
                                    _mat_for_step = {"idMateriel": _selected_cuve_b_id}
                                elif _cuve_dilution and ("preparation" in _etape_nom or "sirop" in _etape_nom):
                                    _mat_for_step = {"idMateriel": _cuve_dilution.get("idMateriel")}

                                _planif_etapes.append({
                                    "produitEtape": {
                                        "idProduitEtape": et.get("idProduitEtape"),
                                        "brassageEtape": et.get("brassageEtape"),
                                        "ordre": et.get("ordre"),
                                        "duree": et.get("duree"),
                                        "unite": et.get("unite"),
                                        "etapeTerminee": False,
                                        "etapeEnCours": False,
                                    },
                                    "materiel": _mat_for_step,
                                })
                        except Exception as e:
                            st.warning(f"Impossible de charger la recette pour « {g} » : {e}")

                        payload = {
                            "nom": _code,
                            "volume": vol_l,
                            "pourcentagePerte": round(_perte_litres / vol_l * 100, 2) if vol_l > 0 else 0,
                            "dateDebutFormulaire": f"{_semaine_du_eb}T07:30:00.000Z",
                            "dateConditionnementPrevue": f"{_date_embouteillage.isoformat()}T23:00:00.000Z",
                            "produit": {"idProduit": id_produit},
                            "type": {"code": "LOCALE"},
                            "deduireMatierePremiere": True,
                            "changementEtapeAutomatique": True,
                        }
                        if _ingredients:
                            payload["ingredients"] = _ingredients
                        if _planif_etapes:
                            payload["planificationsEtapes"] = _planif_etapes

                        try:
                            result = create_brassin(payload)
                            brassin_id = result.get("id", "?")
                            created_ids.append(brassin_id)
                            st.toast(f"Brassin « {g} » créé (ID {brassin_id})")
                        except Exception as e:
                            errors.append(f"{g} : {e}")
                            continue

                        # --- Planification de conditionnement (via endpoint dédié) ---
                        if not isinstance(brassin_id, int) or not _id_entrepot:
                            continue

                        try:
                            _matrice = get_planification_matrice(brassin_id, _id_entrepot)

                            # Index contenants par contenance (volume bouteille)
                            _cont_by_vol: dict[float, list[dict]] = {}
                            for _mc in _matrice.get("contenants", []):
                                _mod = _mc.get("modeleContenant", {})
                                _cap = _mod.get("contenance")
                                if _cap is not None:
                                    _cont_by_vol.setdefault(round(float(_cap), 2), []).append(_mod)

                            # Index packagings : nom court → idLot
                            _pkg_lookup: dict[str, int] = {}
                            for _pk in _matrice.get("packagings", []):
                                _lbl = (_pk.get("libelle") or "").strip().lower()
                                if _lbl and _pk.get("idLot") is not None:
                                    _pkg_lookup[_lbl] = _pk["idLot"]

                            # Construire les éléments depuis df_min
                            _elements = []
                            _df_min_eb = _sp_eb.get("df_min")
                            if isinstance(_df_min_eb, pd.DataFrame) and not _df_min_eb.empty:
                                _rows_gout = _df_min_eb[_df_min_eb["GoutCanon"].astype(str) == g]
                                for _, _r in _rows_gout.iterrows():
                                    _stock = str(_r.get("Stock", "")).strip()
                                    _ct = int(_r.get("Cartons à produire (arrondi)", 0))
                                    if _ct <= 0:
                                        continue

                                    # 1) Packaging : extraire "Carton de N" / "Pack de N"
                                    _pkg_m = re.search(r'((?:carton|pack|caisse|colis)\s+de\s+\d+)', _stock, re.IGNORECASE)
                                    _pkg_name = _pkg_m.group(1).strip().lower() if _pkg_m else ""
                                    _id_lot = None
                                    for _pk_lbl, _pk_id in _pkg_lookup.items():
                                        if _pkg_name and _pkg_name in _pk_lbl:
                                            _id_lot = _pk_id
                                            break

                                    # 2) Contenant : extraire le volume bouteille depuis Stock
                                    _, _vol_btl = _parse_stock(_stock)
                                    _id_cont = None
                                    if _vol_btl is not None and not pd.isna(_vol_btl):
                                        _vol_key = round(float(_vol_btl), 2)
                                        _candidates = _cont_by_vol.get(_vol_key, [])
                                        if len(_candidates) == 1:
                                            _id_cont = _candidates[0].get("idContenant")
                                        elif len(_candidates) > 1:
                                            # Disambiguïté 0.75L : SAFT pour "pack", EAU GAZEUSE sinon
                                            _is_pack = "pack" in _pkg_name
                                            for _c in _candidates:
                                                _c_lbl = (_c.get("libelleAvecContenance") or _c.get("libelle") or "").lower()
                                                if _is_pack and "saft" in _c_lbl:
                                                    _id_cont = _c.get("idContenant")
                                                    break
                                                elif not _is_pack and "saft" not in _c_lbl:
                                                    _id_cont = _c.get("idContenant")
                                                    break
                                            if _id_cont is None:
                                                _id_cont = _candidates[0].get("idContenant")

                                    if _id_cont is not None and _id_lot is not None:
                                        _elements.append({
                                            "idContenant": _id_cont,
                                            "idLot": _id_lot,
                                            "quantite": _ct,
                                        })

                            if _elements:
                                _ddm_iso = _sp_eb.get("ddm", "")
                                add_planification_conditionnement({
                                    "idBrassin": brassin_id,
                                    "idProduit": id_produit,
                                    "idEntrepot": _id_entrepot,
                                    "date": f"{_date_embouteillage.isoformat()}T23:00:00.000Z",
                                    "dateLimiteUtilisationOptimale": f"{_ddm_iso}T00:00:00.000Z" if _ddm_iso else "",
                                    "elements": _elements,
                                })
                                st.toast(f"Planification conditionnement « {g} » ajoutée ✓")

                        except Exception as _pe:
                            st.warning(f"Planification conditionnement « {g} » : {_pe}")

                        # --- Upload de la fiche Excel sur le brassin ---
                        try:
                            _semaine_dt = _dt.date.fromisoformat(_semaine_du_eb)
                            _ddm_dt = _dt.date.fromisoformat(_sp_eb.get("ddm", ""))
                            _sp_vd_eb = _sp_eb.get("volume_details") or {}
                            _vd_eb = _sp_vd_eb.get(g, {})
                            _fiche_bytes = fill_fiche_xlsx(
                                template_path=TEMPLATE_PATH,
                                semaine_du=_semaine_dt,
                                ddm=_ddm_dt,
                                gout1=g,
                                gout2=None,
                                df_calc=_sp_eb.get("df_calc", _df_calc_eb),
                                df_min=_sp_eb.get("df_min", df_min_override),
                                V_start=_vd_eb.get("V_start", 0),
                                tank_capacity=_vd_eb.get("capacity", 7200),
                                transfer_loss=_vd_eb.get("transfer_loss", 400),
                                aromatisation_volume=_vd_eb.get("V_aroma", 0),
                                is_infusion=_vd_eb.get("is_infusion", False),
                                dilution_ingredients=_vd_eb.get("dilution_ingredients"),
                            )
                            _fiche_name = f"Fiche de production — {g} — {_semaine_dt.strftime('%d-%m-%Y')}.xlsx"
                            upload_fichier_brassin(
                                id_brassin=brassin_id,
                                file_bytes=_fiche_bytes,
                                filename=_fiche_name,
                                commentaire=f"Fiche de production {g}",
                            )
                            st.toast(f"Fiche Excel « {g} » uploadée ✓")
                        except Exception as _ue:
                            st.warning(f"Upload fiche « {g} » : {_ue}")

                    if created_ids:
                        if "_eb_brassins_created" not in st.session_state:
                            st.session_state["_eb_brassins_created"] = {}
                        st.session_state["_eb_brassins_created"][_creation_key] = created_ids
                        st.success(f"{len(created_ids)} brassin(s) créé(s) dans EasyBeer (IDs : {', '.join(str(i) for i in created_ids)}).")
                    if errors:
                        for err in errors:
                            st.error(err)


