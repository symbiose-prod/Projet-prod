# pages/07_Analyse_Biocoop.py
"""
Analyse Biocoop — Ferment Station
===================================
Analyse des ventes par produit et par magasin a partir des fichiers Excel Biocoop.
4 onglets : Vue globale, Analyse magasins, Pre-commandes vs Realite, Detail par magasin.
"""
from __future__ import annotations

from common.session import require_login, user_menu, user_menu_footer

user = require_login()
user_menu()

import pandas as pd
import streamlit as st

from common.design import apply_theme, section, kpi
from common.biocoop import (
    CATEGORY_LABELS,
    PLATFORMS,
    KEFIR_CODES,
    parse_monthly_stats,
    parse_precommandes,
    filter_by_category,
    filter_by_platform,
    compute_penetration,
    compute_store_ranking,
    compare_preorder_vs_actual,
    segment_stores,
)

# ================================ Theme ======================================

apply_theme("Analyse Biocoop — Ferment Station", "\U0001F6D2")
section("Analyse Biocoop", "\U0001F6D2")

# ================================ Sidebar ====================================

with st.sidebar:
    st.header("Fichiers")

    uploaded_stats = st.file_uploader(
        "Stats mensuelles (.xlsx)",
        type=["xlsx"],
        key="bio_upload_stats",
        help="Fichier 'Evolution ventes par produit et par magasins' recu de Biocoop.",
    )

    uploaded_preorder = st.file_uploader(
        "Pre-commandes kefir (.xlsx)",
        type=["xlsx"],
        key="bio_upload_preorder",
        help="Fichier de pre-commandes kefir (optionnel, necessaire pour l'onglet comparatif).",
    )

    st.markdown("---")
    st.header("Filtres")

    cat_choice = st.radio(
        "Categorie",
        options=list(CATEGORY_LABELS.keys()),
        index=0,
        key="bio_category",
    )

    pf_choice = st.multiselect(
        "Plateformes",
        options=PLATFORMS,
        default=PLATFORMS,
        key="bio_platforms",
    )

    if st.button("\U0001F504 Recharger", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    user_menu_footer(user)

# ================================ Guard ======================================

if not uploaded_stats:
    st.info(
        "Uploade le fichier **stats mensuelles** Biocoop (Excel .xlsx) dans la sidebar "
        "pour commencer l'analyse."
    )
    st.stop()

# ================================ Parsing ====================================


@st.cache_data(show_spinner="Analyse du fichier stats...")
def _parse_stats(raw: bytes) -> dict:
    return parse_monthly_stats(raw)


@st.cache_data(show_spinner="Analyse du fichier pre-commandes...")
def _parse_preorder(raw: bytes) -> pd.DataFrame:
    return parse_precommandes(raw)


parsed = _parse_stats(uploaded_stats.getvalue())
product_summary: pd.DataFrame = parsed["product_summary"]
store_detail: pd.DataFrame = parsed["store_detail"]
non_ordering: pd.DataFrame = parsed["non_ordering"]
months: list[str] = parsed["months"]

preorder_df: pd.DataFrame | None = None
if uploaded_preorder:
    preorder_df = _parse_preorder(uploaded_preorder.getvalue())

# ================================ Filtres dynamiques ==========================

# Selectbox mois (defaut = dernier)
if months:
    month_choice = st.sidebar.selectbox(
        "Mois",
        options=months,
        index=len(months) - 1,
        key="bio_month",
    )
else:
    month_choice = None

# Appliquer filtres categorie + plateforme
ps_filtered = filter_by_platform(filter_by_category(product_summary, cat_choice), pf_choice) if not product_summary.empty else product_summary
sd_filtered = filter_by_platform(filter_by_category(store_detail, cat_choice), pf_choice) if not store_detail.empty else store_detail
no_filtered = filter_by_platform(filter_by_category(non_ordering, cat_choice), pf_choice) if not non_ordering.empty else non_ordering

# ================================ Tabs ========================================

tab1, tab2, tab3, tab4 = st.tabs([
    "\U0001F4CA Vue globale",
    "\U0001F3EA Analyse magasins",
    "\U0001F4E6 Pre-commandes vs Realite",
    "\U0001F50D Detail par magasin",
])

# ─── Tab 1 : Vue globale ────────────────────────────────────────────────────

with tab1:
    if ps_filtered.empty:
        st.info("Aucune donnee produit pour les filtres selectionnes.")
    else:
        # KPIs globaux
        last_m = month_choice or (months[-1] if months else None)
        qty_col = f"{last_m}_qty" if last_m else "CumulN_qty"
        dn_col = f"{last_m}_dn" if last_m else "CumulN_dn"
        vmm_col = f"{last_m}_vmm" if last_m else "CumulN_vmm"

        total_qty = int(ps_filtered[qty_col].sum()) if qty_col in ps_filtered.columns else int(ps_filtered["CumulN_qty"].sum())
        avg_dn = ps_filtered[dn_col].mean() if dn_col in ps_filtered.columns else ps_filtered["CumulN_dn"].mean()
        avg_vmm = ps_filtered[vmm_col].mean() if vmm_col in ps_filtered.columns else ps_filtered["CumulN_vmm"].mean()
        nb_actifs = int((ps_filtered[qty_col] > 0).sum()) if qty_col in ps_filtered.columns else int((ps_filtered["CumulN_qty"] > 0).sum())

        k1, k2, k3, k4 = st.columns(4)
        with k1:
            kpi("Volume total", f"{total_qty:,}".replace(",", " "))
        with k2:
            kpi("DN moyen", f"{avg_dn:.1f}")
        with k3:
            kpi("VMM moyen", f"{avg_vmm:.1f}")
        with k4:
            kpi("Produits actifs", str(nb_actifs))

        st.markdown("####")

        # Tableau produits
        display_cols_ps = ["CodeProduit", "LibelleProduit"]
        if last_m:
            for suffix in ["_qty", "_dn", "_vmm"]:
                c = f"{last_m}{suffix}"
                if c in ps_filtered.columns:
                    display_cols_ps.append(c)
        # Toujours ajouter cumuls
        for c in ["CumulN_qty", "CumulN_dn", "CumulN_vmm"]:
            if c in ps_filtered.columns and c not in display_cols_ps:
                display_cols_ps.append(c)

        # Evolution si > 1 mois
        if len(months) > 1 and last_m:
            prev_m = months[-2] if months.index(last_m) > 0 else months[0]
            prev_qty = f"{prev_m}_qty"
            curr_qty = f"{last_m}_qty"
            if prev_qty in ps_filtered.columns and curr_qty in ps_filtered.columns:
                ps_display = ps_filtered.copy()
                ps_display["Evolution %"] = ps_display.apply(
                    lambda r: (
                        round((r[curr_qty] - r[prev_qty]) / r[prev_qty] * 100, 1)
                        if r[prev_qty] > 0 else None
                    ),
                    axis=1,
                )
                display_cols_ps.append("Evolution %")
            else:
                ps_display = ps_filtered
        else:
            ps_display = ps_filtered

        rename_map = {
            "CodeProduit": "Code",
            "LibelleProduit": "Produit",
            "CumulN_qty": "Cumul Qty",
            "CumulN_dn": "Cumul DN",
            "CumulN_vmm": "Cumul VMM",
        }
        if last_m:
            rename_map[f"{last_m}_qty"] = f"Qty {last_m}"
            rename_map[f"{last_m}_dn"] = f"DN {last_m}"
            rename_map[f"{last_m}_vmm"] = f"VMM {last_m}"

        available_cols = [c for c in display_cols_ps if c in ps_display.columns]
        df_show = ps_display[available_cols].rename(columns=rename_map)
        st.dataframe(df_show, use_container_width=True, hide_index=True)

        # Graphe evolution volumes par mois (si plusieurs mois)
        if len(months) > 1:
            st.markdown("#### Evolution des volumes par mois")
            chart_data = []
            for m in months:
                col = f"{m}_qty"
                if col in ps_filtered.columns:
                    for _, row in ps_filtered.iterrows():
                        chart_data.append({
                            "Mois": m,
                            "Produit": str(row["LibelleProduit"])[:30],
                            "Quantite": row[col],
                        })
            if chart_data:
                chart_df = pd.DataFrame(chart_data)
                pivot = chart_df.pivot_table(
                    index="Mois", columns="Produit", values="Quantite", aggfunc="sum"
                ).fillna(0)
                st.bar_chart(pivot)

# ─── Tab 2 : Analyse magasins ───────────────────────────────────────────────

with tab2:
    if sd_filtered.empty:
        st.info("Aucune donnee magasin pour les filtres selectionnes.")
    else:
        # Ranking magasins
        ranking = compute_store_ranking(sd_filtered, months)

        if not ranking.empty:
            # KPIs
            nb_magasins_actifs = len(ranking[ranking["QtyDernierMois"] > 0])
            total_magasins = len(ranking) + (len(no_filtered["CodeClient"].unique()) if not no_filtered.empty else 0)
            pct_penetration = round(nb_magasins_actifs / total_magasins * 100, 1) if total_magasins > 0 else 0
            panier_moyen = int(ranking["TotalQty"].sum() / nb_magasins_actifs) if nb_magasins_actifs > 0 else 0

            k1, k2, k3 = st.columns(3)
            with k1:
                kpi("Magasins actifs", str(nb_magasins_actifs))
            with k2:
                kpi("Taux de penetration", f"{pct_penetration}%")
            with k3:
                kpi("Panier moyen", f"{panier_moyen:,}".replace(",", " "))

            st.markdown("####")

            # Top 20
            st.markdown("#### Top 20 magasins par volume")
            top20 = ranking.head(20)[["NomClient", "Ville", "Plateforme", "TotalQty", "NbProduits", "QtyDernierMois"]].rename(
                columns={
                    "NomClient": "Magasin",
                    "TotalQty": "Qty totale",
                    "NbProduits": "Nb produits",
                    "QtyDernierMois": "Qty dernier mois",
                }
            )
            st.dataframe(top20, use_container_width=True, hide_index=True)

        # Penetration par plateforme
        st.markdown("#### Penetration par plateforme")
        cat_codes = CATEGORY_LABELS.get(cat_choice)
        pen = compute_penetration(sd_filtered, no_filtered, months, product_codes=cat_codes)
        if not pen.empty:
            pen_by_pf = pen.groupby("Plateforme").agg(
                TotalMagasins=("TotalMagasins", "sum"),
                MagasinsActifs=("MagasinsActifs", "sum"),
            ).reset_index()
            pen_by_pf["Penetration %"] = (pen_by_pf["MagasinsActifs"] / pen_by_pf["TotalMagasins"] * 100).round(1)
            pen_by_pf = pen_by_pf.rename(columns={
                "TotalMagasins": "Total magasins",
                "MagasinsActifs": "Magasins actifs",
            })
            st.dataframe(pen_by_pf, use_container_width=True, hide_index=True)
        else:
            st.caption("Pas assez de donnees pour le calcul de penetration.")

        # Segmentation
        st.markdown("#### Segmentation magasins")
        is_kefir = cat_choice == "Kefirs (frais)"
        seg_cat = "kefir" if is_kefir else "infusion"
        seg_pre = preorder_df if is_kefir else None
        seg_df = segment_stores(sd_filtered, no_filtered, seg_pre, months, category=seg_cat)
        if not seg_df.empty:
            seg_counts = seg_df["Segment"].value_counts().reset_index()
            seg_counts.columns = ["Segment", "Nb magasins"]

            cols = st.columns(len(seg_counts))
            for i, (_, row) in enumerate(seg_counts.iterrows()):
                with cols[i % len(cols)]:
                    kpi(row["Segment"], str(row["Nb magasins"]))
        else:
            st.caption("Pas assez de donnees pour la segmentation.")


# ─── Tab 3 : Pre-commandes vs Realite ───────────────────────────────────────

with tab3:
    is_kefir_tab3 = cat_choice in ("Tous", "Kefirs (frais)")

    if preorder_df is None:
        st.info(
            "Uploade le fichier **pre-commandes kefir** dans la sidebar pour "
            "comparer les pre-commandes avec les ventes reelles."
        )
    elif not is_kefir_tab3:
        st.info(
            "Cet onglet est disponible uniquement pour les **Kefirs**. "
            "Change le filtre categorie dans la sidebar."
        )
    elif store_detail.empty:
        st.info("Aucune donnee de vente disponible.")
    else:
        # Filtrer store_detail sur kefirs seulement (meme si cat = Tous)
        sd_kefir = filter_by_platform(
            store_detail[store_detail["CodeProduit"].isin(KEFIR_CODES)].copy(),
            pf_choice,
        )
        comparison = compare_preorder_vs_actual(preorder_df, sd_kefir, months)

        if comparison.empty:
            st.warning("Impossible de croiser les donnees pre-commandes / ventes.")
        else:
            # KPIs
            nb_pre = int((comparison["QtyPrecommande"] > 0).sum())
            nb_actifs = int((comparison["QtyDernierMois"] > 0).sum())
            nb_pre_actifs = int(((comparison["QtyPrecommande"] > 0) & (comparison["QtyDernierMois"] > 0)).sum())
            taux_reachat = round(nb_pre_actifs / nb_pre * 100, 1) if nb_pre > 0 else 0
            nb_perdus = int(comparison["Statut"].str.contains("Perdus").sum())

            k1, k2, k3, k4 = st.columns(4)
            with k1:
                kpi("Magasins pre-commande", str(nb_pre))
            with k2:
                kpi("Actifs (dernier mois)", str(nb_actifs))
            with k3:
                kpi("Taux de reachat", f"{taux_reachat}%")
            with k4:
                kpi("Perdus", str(nb_perdus))

            st.markdown("####")

            # Tableau comparatif
            st.markdown("#### Comparatif par magasin")
            comp_display = comparison[["CodeClient", "NomClient", "QtyPrecommande", "QtyTotale", "QtyDernierMois", "Statut"]].rename(
                columns={
                    "CodeClient": "Code",
                    "NomClient": "Magasin",
                    "QtyPrecommande": "Pre-commande",
                    "QtyTotale": "Qty totale",
                    "QtyDernierMois": "Qty dernier mois",
                }
            )
            st.dataframe(comp_display, use_container_width=True, hide_index=True)

            # Sections highlight
            perdus = comparison[comparison["Statut"].str.contains("Perdus")]
            nouveaux = comparison[comparison["Statut"].str.contains("Nouveaux")]

            if not perdus.empty:
                with st.expander(f"\U0001F534 Magasins perdus ({len(perdus)})", expanded=False):
                    st.caption("Ont pre-commande mais n'ont jamais rachete.")
                    st.dataframe(
                        perdus[["CodeClient", "NomClient", "QtyPrecommande"]].rename(
                            columns={"CodeClient": "Code", "NomClient": "Magasin", "QtyPrecommande": "Pre-commande"}
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )

            if not nouveaux.empty:
                with st.expander(f"\U0001F535 Nouveaux magasins ({len(nouveaux)})", expanded=False):
                    st.caption("Achetent maintenant sans avoir pre-commande.")
                    st.dataframe(
                        nouveaux[["CodeClient", "NomClient", "QtyTotale", "QtyDernierMois"]].rename(
                            columns={"CodeClient": "Code", "NomClient": "Magasin", "QtyTotale": "Qty totale", "QtyDernierMois": "Qty dernier mois"}
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )


# ─── Tab 4 : Detail par magasin ─────────────────────────────────────────────

with tab4:
    if sd_filtered.empty:
        st.info("Aucune donnee magasin pour les filtres selectionnes.")
    else:
        # Liste unique de magasins
        magasins = sd_filtered.drop_duplicates("CodeClient")[["CodeClient", "NomClient", "Ville", "CP", "Plateforme"]].sort_values("NomClient")
        mag_options = [f"{r['NomClient']} ({r['Ville']}) — {r['CodeClient']}" for _, r in magasins.iterrows()]

        if not mag_options:
            st.info("Aucun magasin disponible.")
        else:
            selected_mag = st.selectbox(
                "Selectionner un magasin",
                options=mag_options,
                key="bio_mag_select",
            )

            # Extraire le code client du label
            code_client = selected_mag.split(" — ")[-1].strip() if selected_mag else None

            if code_client:
                mag_info = magasins[magasins["CodeClient"] == code_client].iloc[0]

                # Fiche magasin
                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    kpi("Magasin", str(mag_info["NomClient"]))
                with c2:
                    kpi("Ville", f"{mag_info['CP']} {mag_info['Ville']}")
                with c3:
                    kpi("Plateforme", str(mag_info["Plateforme"]))
                with c4:
                    # Premiere commande
                    mag_data = sd_filtered[sd_filtered["CodeClient"] == code_client]
                    first_month = None
                    for m in months:
                        col = f"{m}_qty"
                        if col in mag_data.columns and (mag_data[col] > 0).any():
                            first_month = m
                            break
                    kpi("1ere commande", first_month or "—")

                st.markdown("####")

                # Tableau produits x mois
                st.markdown("#### Detail par produit")
                mag_detail = sd_filtered[sd_filtered["CodeClient"] == code_client].copy()

                if mag_detail.empty:
                    st.caption("Aucune commande pour ce magasin.")
                else:
                    detail_cols = ["CodeProduit", "LibelleProduit"]
                    for m in months:
                        col = f"{m}_qty"
                        if col in mag_detail.columns:
                            detail_cols.append(col)
                    if "CumulN_qty" in mag_detail.columns:
                        detail_cols.append("CumulN_qty")

                    rename_detail = {
                        "CodeProduit": "Code",
                        "LibelleProduit": "Produit",
                        "CumulN_qty": "Cumul",
                    }
                    for m in months:
                        rename_detail[f"{m}_qty"] = f"Qty {m}"

                    available = [c for c in detail_cols if c in mag_detail.columns]
                    st.dataframe(
                        mag_detail[available].rename(columns=rename_detail),
                        use_container_width=True,
                        hide_index=True,
                    )

                    # Total magasin
                    total_mag = int(mag_detail[[c for c in mag_detail.columns if c.endswith("_qty")]].sum().sum())
                    st.caption(f"**Volume total : {total_mag:,}** unites".replace(",", " "))
