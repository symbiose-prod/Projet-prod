from __future__ import annotations
from common.session import require_login, user_menu, user_menu_footer
user = require_login()  # stoppe la page si non connecté
user_menu()             # affiche l’info utilisateur + bouton logout dans la sidebar

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
from common.xlsx_fill import fill_fiche_7000L_xlsx
from common.storage import (
    list_saved, save_snapshot, load_snapshot, delete_snapshot, MAX_SLOTS
)

# ====== Réglages modèle Excel ======
# Mapping entre le choix UI et le fichier modèle à utiliser
TEMPLATE_MAP = {
    "Cuve de 7000L": "assets/Grande.xlsx",   # anciennement "Fiche de Prod 250620.xlsx"
    "Cuve de 5000L": "assets/Petite.xlsx",
}
SHEET_NAME = None  # laisse None si le modèle a une feuille active par défaut

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
    volume_cible = st.number_input("Volume cible (hL)", 1.0, 1000.0, 64.0, 1.0)
    nb_gouts = st.selectbox("Nombre de goûts simultanés", [1, 2], index=0)
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
        help="Les goûts sélectionnés ici seront produits quoi qu’il arrive. "
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

# ✅ Affiche la note d’ajustement (ex: contrainte Infusion/Kéfir)
if isinstance(note_msg, str) and note_msg.strip():
    st.info(note_msg)


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

# Sélecteur de modèle (taille de cuve)
cuve_choice = st.radio(
    "Modèle de fiche",
    options=["Cuve de 7000L", "Cuve de 5000L"],
    horizontal=True,
    help="Choisis le modèle de fiche à générer. Les données (cartons/DDM) viennent de la proposition sauvegardée."
)

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
        "df_min": df_min_override.copy(),   # <<< ici (avec overrides appliqués)
        "df_calc": df_calc.copy(),
        "gouts": g_order,
        "semaine_du": date_debut.isoformat(),
        "ddm": date_ddm.isoformat(),
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
    # Déduction auto des 2 premiers goûts (si ta fiche a 2 colonnes de goût)
    g1, g2 = _two_gouts_auto(sp, sp.get("df_min", df_min_override), gouts_cibles)

    template_path = TEMPLATE_MAP.get(cuve_choice)
    if not template_path or not os.path.exists(template_path):
        st.error(
            f"Modèle introuvable pour **{cuve_choice}**. "
            f"Place le fichier **{template_path}** dans le repo."
        )
    else:
        try:
            # 👉 On ré-utilise la même fonction de remplissage : elle accepte un template_path générique
            xlsx_bytes = fill_fiche_7000L_xlsx(
                template_path=template_path,
                semaine_du=_dt.date.fromisoformat(sp["semaine_du"]),
                ddm=_dt.date.fromisoformat(sp["ddm"]),
                gout1=g1 or "",
                gout2=g2,
                df_calc=sp.get("df_calc", df_calc),
                sheet_name=SHEET_NAME,
                df_min=sp.get("df_min", df_min_override),
            )

            semaine_label = _dt.date.fromisoformat(sp["semaine_du"]).strftime("%d-%m-%Y")
            fname_xlsx = f"Fiche de production — {cuve_choice} — {semaine_label}.xlsx"

            st.download_button(
                "📄 Télécharger la fiche (XLSX)",
                data=xlsx_bytes,
                file_name=fname_xlsx,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        except FileNotFoundError:
            st.error("Modèle introuvable. Vérifie le chemin du fichier modèle.")
        except Exception as e:
            st.error(f"Erreur lors du remplissage du modèle : {e}")
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
        # --- Volume de perte selon la cuve choisie ---
        # Cuve 7000L (réelle : 7200L) → +800L de perte
        # Cuve 5000L (réelle : 5200L) → +400L de perte
        _perte_litres = 800 if cuve_choice == "Cuve de 7000L" else 400

        # --- Calcul du volume par goût ---
        # On utilise volume_cible (sidebar) réparti proportionnellement entre goûts
        # puis on ajoute la perte par brassin.
        # Ex: volume_cible=64 hL, 1 goût → 64 hL → 6400L + 800L = 7200L
        _vol_par_gout: dict[str, float] = {}
        _nb_gouts_eb = len(_gouts_eb)

        if _nb_gouts_eb == 1:
            # Un seul goût : tout le volume_cible
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

        # --- Récupérer produits EasyBeer (cachés 5 min) ---
        @st.cache_data(ttl=300, show_spinner="Chargement des produits EasyBeer…")
        def _fetch_eb_products():
            from common.easybeer import get_all_products
            return get_all_products()

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
            st.caption(f"Volume = volume cartons + {_perte_litres} L de perte ({cuve_choice})")

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
                    from common.easybeer import create_brassin
                    created_ids = []
                    errors = []
                    for g in _gouts_eb:
                        vol_l = _vol_par_gout.get(g, 0)
                        payload = {
                            "nom": f"Brassin {g} — {_semaine_du_eb}",
                            "volume": vol_l,
                            "dateDebutFormulaire": f"{_semaine_du_eb}T00:00:00.000Z",
                            "dateConditionnementPrevue": f"{_date_embouteillage.isoformat()}T23:00:00.000Z",
                            "produit": {"idProduit": _selected_products[g]},
                            "type": {"code": "LOCALE"},
                            "deduireMatierePremiere": True,
                            "changementEtapeAutomatique": True,
                        }
                        try:
                            result = create_brassin(payload)
                            brassin_id = result.get("id", "?")
                            created_ids.append(brassin_id)
                            st.toast(f"Brassin « {g} » créé (ID {brassin_id})")
                        except Exception as e:
                            errors.append(f"{g} : {e}")

                    if created_ids:
                        if "_eb_brassins_created" not in st.session_state:
                            st.session_state["_eb_brassins_created"] = {}
                        st.session_state["_eb_brassins_created"][_creation_key] = created_ids
                        st.success(f"{len(created_ids)} brassin(s) créé(s) dans EasyBeer (IDs : {', '.join(str(i) for i in created_ids)}).")
                    if errors:
                        for err in errors:
                            st.error(err)

# ================== Mémoire longue (persistante, 4 entrées max) ==================
st.subheader("Mémoire longue — propositions enregistrées")
st.caption(f"Tu peux garder jusqu’à **{MAX_SLOTS}** propositions nommées, persistantes entre sessions.")

coln1, coln2 = st.columns([2,1])
default_name = ""
if "saved_production" in st.session_state:
    # nom par défaut : semaine du JJ-MM-YYYY + 2 premiers goûts
    _sp = st.session_state["saved_production"]
    try:
        sd = _dt.date.fromisoformat(_sp["semaine_du"]).strftime("%d-%m-%Y")
        g1 = (_sp.get("gouts") or [""])[0] if _sp.get("gouts") else ""
        g2 = (_sp.get("gouts") or ["",""])[1] if _sp.get("gouts") else ""
        default_name = f"{sd} — {g1}{(' + ' + g2) if g2 else ''}"
    except Exception:
        default_name = ""

with coln1:
    name_input = st.text_input("Nom de la proposition", value=default_name, placeholder="ex: 21-10-2025 — Gingembre + Mangue")
with coln2:
    if st.button("📌 Enregistrer dans la mémoire", use_container_width=True):
        sp_cur = st.session_state.get("saved_production")
        if not sp_cur:
            st.error("Sauvegarde d’abord la production ci-dessus (bouton 💾).")
        else:
            ok, msg = save_snapshot(name_input, sp_cur)
            (st.success if ok else st.error)(msg)

saved_list = list_saved()
if saved_list:
    labels = [f"{it['name']} — ({it['semaine_du']})" if it.get("semaine_du") else it["name"] for it in saved_list]
    sel = st.selectbox("Sélectionne une proposition enregistrée", options=labels, index=0)
    idx = labels.index(sel)
    picked = saved_list[idx]["name"]

    # -------- Aperçu de la proposition sélectionnée (df_min sauvegardé) --------
    sp_preview = load_snapshot(picked)
    if sp_preview and isinstance(sp_preview.get("df_min"), pd.DataFrame) and not sp_preview["df_min"].empty:
        with st.expander("👀 Aperçu de la proposition sélectionnée", expanded=False):
            prev_df = sp_preview["df_min"].copy()

            # Petits KPIs (comme pour le tableau courant)
            prev_total_btl = int(pd.to_numeric(prev_df.get("Bouteilles à produire (arrondi)"), errors="coerce").fillna(0).sum()) if "Bouteilles à produire (arrondi)" in prev_df.columns else 0
            prev_total_vol = float(pd.to_numeric(prev_df.get("Volume produit arrondi (hL)"), errors="coerce").fillna(0).sum()) if "Volume produit arrondi (hL)" in prev_df.columns else 0.0
            pk1, pk2, pk3 = st.columns(3)
            with pk1: kpi("Total bouteilles (sauvegardé)", f"{prev_total_btl:,}".replace(",", " "))
            with pk2: kpi("Volume total (hL, sauvegardé)", f"{prev_total_vol:.2f}")
            with pk3: kpi("Lignes", f"{len(prev_df)}")

            # Image facultative comme dans le tableau principal
            prev_df["_SKU?"] = prev_df["Produit"].apply(sku_guess)
            prev_df["__img_path"] = [
                find_image_path(images_dir, sku=sku_guess(p), flavor=g)
                for p, g in zip(prev_df["Produit"], prev_df.get("GoutCanon", pd.Series(dtype=str)))
            ]
            prev_df["Image"] = prev_df["__img_path"].apply(load_image_bytes)

            st.data_editor(
                prev_df[[
                    "Image","GoutCanon","Produit","Stock",
                    "Cartons à produire (arrondi)","Bouteilles à produire (arrondi)",
                    "Volume produit arrondi (hL)"
                ]],
                use_container_width=True,
                hide_index=True,
                disabled=True,
                column_config={
                    "Image": st.column_config.ImageColumn("Image", width="small"),
                    "GoutCanon": "Goût",
                    "Volume produit arrondi (hL)": st.column_config.NumberColumn(format="%.2f"),
                },
            )
    else:
        st.info("Aperçu indisponible pour cette proposition (df_min manquant ou vide).")

    # -------- Actions --------
    col_load, col_del, col_count = st.columns(3)
    with col_load:
        if st.button("▶️ Charger", use_container_width=True):
            sp_loaded = load_snapshot(picked)
            if sp_loaded:
                st.session_state["saved_production"] = sp_loaded
                st.success(f"Chargé : {picked}")

    with col_del:
        if st.button("🗑️ Supprimer", use_container_width=True):
            if delete_snapshot(picked):
                st.success("Supprimé.")
            else:
                st.error("Échec suppression.")

    with col_count:
        st.metric("Propositions stockées", f"{len(saved_list)}/{MAX_SLOTS}")
else:
    st.info("Aucune proposition enregistrée pour l’instant.")


