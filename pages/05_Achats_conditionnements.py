"""
pages/05_Achats_conditionnements.py
====================================
Refonte v2 — Durée de stock (produits finis + composants d'emballage)
Données 100% automatiques via API Easy Beer — plus d'upload manuel.

Flux de données :
  1. POST /indicateur/autonomie-stocks         → autonomie jours produits finis
  2. GET  /stock/matieres-premieres/all        → stock actuel composants MP
  3. POST /indicateur/synthese-consommations-mp → consommation par composant sur période

Durée de stock composant = quantiteVirtuelle / (quantite_consommée / nb_jours_période)
"""
from __future__ import annotations

import datetime
import unicodedata
import re

import numpy as np
import pandas as pd
import streamlit as st

from common.session import require_login, user_menu, user_menu_footer
from common.design import apply_theme, section, kpi
import common.easybeer as eb

# ─── Auth ──────────────────────────────────────────────────────────────────────
user = require_login()
user_menu()

apply_theme("Achats — Conditionnements", "📦")
section("Achats — Conditionnements", "📦")

# ─── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Paramètres")
    window_days = st.number_input(
        "Fenêtre de calcul (jours)",
        min_value=7, max_value=365, value=30, step=1,
        help="Période utilisée pour calculer la vitesse de consommation des composants."
    )
    horizon_j = st.number_input(
        "Horizon commande (jours)",
        min_value=1, max_value=365, value=30, step=1,
        help="Nombre de jours à couvrir avec la commande recommandée."
    )
    st.markdown("---")
    st.subheader("🚦 Seuils d'alerte")
    seuil_rouge  = st.number_input("🔴 Critique (< X jours)", min_value=1, max_value=90,  value=14, step=1)
    seuil_orange = st.number_input("🟡 Attention (< X jours)", min_value=1, max_value=180, value=30, step=1)
    st.markdown("---")
    st.subheader("🔍 Filtres composants")
    show_contenants = st.checkbox(
        "Inclure les bouteilles vides (contenants)",
        value=True,
        help="Ajoute les bouteilles vides (syntheseContenant) aux composants."
    )
    masquer_sans_conso = st.checkbox(
        "Masquer composants sans consommation",
        value=False,
        help="Cache les MP dont la consommation sur la période est nulle."
    )

# ─── Helpers ───────────────────────────────────────────────────────────────────
def _status_icon(days: float) -> str:
    if days >= seuil_orange:           return "🟢"
    if days > seuil_rouge:             return "🟡"
    return "🔴"

def _fmt_days(days: float) -> str:
    if days == float("inf") or days > 9990: return "∞"
    return f"{days:.0f}"

def _norm(s: str) -> str:
    s = str(s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s

# ─── Config check ──────────────────────────────────────────────────────────────
if not eb.is_configured():
    st.error(
        "⚠️ Variables d'environnement **EASYBEER_API_USER** et **EASYBEER_API_PASS** "
        "non configurées. Ajoute-les dans le `.env` du VPS."
    )
    user_menu_footer(user)
    st.stop()

# ─── Bouton de synchronisation ─────────────────────────────────────────────────
col_btn, col_info = st.columns([1, 3])
with col_btn:
    do_sync = st.button(
        "🔄 Synchroniser Easy Beer",
        type="primary",
        use_container_width=True,
        help="Récupère les données de stock et de consommation depuis Easy Beer."
    )

if do_sync:
    progress = st.progress(0, text="Connexion à Easy Beer…")
    errors   = []

    try:
        progress.progress(20, text="📊 Autonomie produits finis…")
        st.session_state["eb_autonomie"] = eb.get_autonomie_stocks(window_days=int(window_days))
    except Exception as e:
        errors.append(f"Autonomie stocks : {e}")
        st.session_state.pop("eb_autonomie", None)

    try:
        progress.progress(50, text="📦 Stocks matières premières…")
        st.session_state["eb_mp"] = eb.get_mp_all(status="actif")
    except Exception as e:
        errors.append(f"Stocks MP : {e}")
        st.session_state.pop("eb_mp", None)

    try:
        progress.progress(80, text="🔄 Synthèse consommations MP…")
        st.session_state["eb_conso_mp"] = eb.get_synthese_consommations_mp(window_days=int(window_days))
    except Exception as e:
        errors.append(f"Consommations MP : {e}")
        st.session_state.pop("eb_conso_mp", None)

    progress.progress(100, text="Terminé.")
    st.session_state["eb_window_days"] = int(window_days)
    st.session_state["eb_sync_time"]   = datetime.datetime.now()

    if errors:
        for err in errors:
            st.error(f"❌ {err}")
        st.stop()           # ← on reste sur la page pour lire les erreurs
    else:
        st.success("✅ Synchronisation réussie.")
        st.rerun()          # ← rerun uniquement si tout est OK

# ─── Vérification données en session ───────────────────────────────────────────
if "eb_autonomie" not in st.session_state:
    st.info("👆 Clique sur **Synchroniser Easy Beer** pour charger les données.")
    user_menu_footer(user)
    st.stop()

# Infos sync
eb_window  = st.session_state.get("eb_window_days", int(window_days))
sync_time  = st.session_state.get("eb_sync_time")
with col_info:
    if sync_time:
        age_min = int((datetime.datetime.now() - sync_time).total_seconds() / 60)
        st.caption(
            f"Dernière sync : **{sync_time.strftime('%d/%m/%Y %H:%M')}** "
            f"({age_min} min) — fenêtre : **{eb_window} j**"
        )

# ─── Debug global ──────────────────────────────────────────────────────────────
with st.expander("🐛 Debug — Réponses brutes Easy Beer", expanded=False):
    import json as _json

    st.subheader("autonomie-stocks (produits finis)")
    raw_auto = st.session_state.get("eb_autonomie", {})
    st.caption(f"Type : {type(raw_auto).__name__} | Clés : {list(raw_auto.keys()) if isinstance(raw_auto, dict) else 'N/A'}")
    nb_prod = len(raw_auto.get("produits", [])) if isinstance(raw_auto, dict) else 0
    st.caption(f"Nb produits : {nb_prod}")
    if nb_prod:
        st.json(raw_auto.get("produits", [])[:2])   # 2 premiers pour aperçu

    st.subheader("stock/matieres-premieres/all")
    raw_mp = st.session_state.get("eb_mp", [])
    st.caption(f"Type : {type(raw_mp).__name__} | Nb MP : {len(raw_mp) if isinstance(raw_mp, list) else 'N/A'}")
    if isinstance(raw_mp, list) and raw_mp:
        st.json(raw_mp[:2])   # 2 premiers
    elif isinstance(raw_mp, dict):
        st.json(raw_mp)

    st.subheader("synthese-consommations-mp")
    raw_conso = st.session_state.get("eb_conso_mp", {})
    st.caption(f"Type : {type(raw_conso).__name__} | Clés : {list(raw_conso.keys()) if isinstance(raw_conso, dict) else 'N/A'}")
    elems_cond = raw_conso.get("syntheseConditionnement", {}).get("elements", []) if isinstance(raw_conso, dict) else []
    st.caption(f"Nb éléments syntheseConditionnement : {len(elems_cond)}")
    if elems_cond:
        st.json(elems_cond[:3])

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — PRODUITS FINIS (autonomie Easy Beer)
# ═══════════════════════════════════════════════════════════════════════════════
section("🍺 Durée de stock — Produits finis", "📊")

autonomie_data = st.session_state.get("eb_autonomie", {})
produits_raw   = autonomie_data.get("produits", [])

rows_pf = []
for p in produits_raw:
    libelle  = p.get("libelle", "?")
    autonomie = p.get("autonomie")    # None si pas calculé au niveau global
    stocks_detail = p.get("stocksProduits", [])

    if stocks_detail:
        # Détail par contenant (12x33, 6x75, etc.)
        for s in stocks_detail:
            auto_s = s.get("autonomie")
            qty_s  = s.get("quantiteVirtuelle") or s.get("quantite") or 0
            vol_s  = s.get("volumeVirtuel") or s.get("volume") or 0
            lib_s  = s.get("libelle") or libelle
            if auto_s is None:
                auto_s = float("inf")
            rows_pf.append({
                "Produit":            lib_s,
                "Stock (unités)":     int(round(qty_s)),
                "Volume (hL)":        round(float(vol_s), 2),
                "Durée (jours)":      round(float(auto_s), 1) if auto_s != float("inf") else 9999,
                "Statut":             _status_icon(float(auto_s)),
                "_sort":              float(auto_s),
            })
    else:
        # Niveau agrégé
        qty  = p.get("quantiteVirtuelle") or p.get("quantite") or 0
        vol  = p.get("volumeVirtuel") or p.get("volume") or 0
        auto = autonomie if autonomie is not None else float("inf")
        rows_pf.append({
            "Produit":        libelle,
            "Stock (unités)": int(round(qty)),
            "Volume (hL)":    round(float(vol), 2),
            "Durée (jours)":  round(float(auto), 1) if auto != float("inf") else 9999,
            "Statut":         _status_icon(float(auto)),
            "_sort":          float(auto),
        })

if rows_pf:
    df_pf = pd.DataFrame(rows_pf).sort_values("_sort").drop(columns=["_sort"]).reset_index(drop=True)

    # KPIs
    crit_pf = int((df_pf["Durée (jours)"] < seuil_rouge).sum())
    att_pf  = int(((df_pf["Durée (jours)"] >= seuil_rouge) & (df_pf["Durée (jours)"] < seuil_orange)).sum())
    ok_pf   = int((df_pf["Durée (jours)"] >= seuil_orange).sum())

    c1, c2, c3 = st.columns(3)
    with c1: kpi(f"🔴 Critique (< {seuil_rouge}j)",    str(crit_pf))
    with c2: kpi(f"🟡 Attention (< {seuil_orange}j)",  str(att_pf))
    with c3: kpi("🟢 OK",                              str(ok_pf))

    # Remplace 9999 par ∞ pour l'affichage
    df_pf_display = df_pf.copy()
    df_pf_display["Durée (jours)"] = df_pf_display["Durée (jours)"].apply(
        lambda x: "∞" if x >= 9999 else str(int(x))
    )
    st.dataframe(
        df_pf_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Stock (unités)": st.column_config.NumberColumn(format="%d"),
            "Volume (hL)":    st.column_config.NumberColumn(format="%.2f"),
        }
    )
else:
    st.info("Aucun produit fini trouvé dans la réponse Easy Beer.")

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — COMPOSANTS D'EMBALLAGE
# ═══════════════════════════════════════════════════════════════════════════════
section("📦 Durée de stock — Composants d'emballage", "📦")

mp_all    = st.session_state.get("eb_mp", [])
conso_mp  = st.session_state.get("eb_conso_mp", {})

# ── Construction du dict de consommation (id + libellé → quantité) ──────────
# On prend syntheseConditionnement + syntheseContenant (si option cochée)
conso_elements: list[dict] = []
conso_elements += conso_mp.get("syntheseConditionnement", {}).get("elements", [])
if show_contenants:
    conso_elements += conso_mp.get("syntheseContenant", {}).get("elements", [])

conso_by_id:  dict[int, dict]  = {}
conso_by_lib: dict[str, dict]  = {}
for elem in conso_elements:
    mid  = elem.get("idMatierePremiere")
    lib  = _norm(elem.get("libelle", ""))
    qty  = float(elem.get("quantite") or 0)
    unite = str(elem.get("unite") or "")
    info  = {"quantite": qty, "unite": unite}
    if mid:
        conso_by_id[int(mid)] = info
    if lib:
        # On accumule si même libellé normalisé
        if lib in conso_by_lib:
            conso_by_lib[lib]["quantite"] += qty
        else:
            conso_by_lib[lib] = info.copy()

# ── Filtrage des MP : garder CONDITIONNEMENT + CONTENANT ────────────────────
def _is_packaging(mp: dict) -> bool:
    """True si cette MP est un composant d'emballage (pas un ingrédient de brassage)."""
    tp = mp.get("type") or {}
    code = str(tp.get("code") or "").upper()
    lib  = str(tp.get("libelle") or "").upper()
    # On inclut tout ce qui n'est PAS un ingrédient/levure/houblon/malt
    excluded = {"INGREDIENT", "LEVURE", "HOUBLON", "MALT", "SUCRE", "FRUIT",
                "EAU", "ACID", "ENZYME", "FININGS", "ADJUVANT"}
    if any(ex in code for ex in excluded): return False
    if any(ex in lib  for ex in excluded): return False
    return True

# Si on a des éléments dans la synthèse, on se base dessus pour filtrer
# Sinon on prend tout ce qui n'est pas un ingrédient
conso_ids = set(conso_by_id.keys())

rows_comp = []
for mp in mp_all:
    mid   = mp.get("idMatierePremiere")
    lib   = mp.get("libelle", "?")
    qty_v = float(mp.get("quantiteVirtuelle") or mp.get("quantite") or 0)
    seuil = float(mp.get("seuilBas") or 0)
    unite_obj = mp.get("unite") or {}
    unite = str(unite_obj.get("symbole") or unite_obj.get("nom") or "")
    tp    = (mp.get("type") or {})
    type_lib = str(tp.get("libelle") or tp.get("code") or "")

    # Chercher la consommation (par ID d'abord, puis par libellé normalisé)
    conso_info = (
        conso_by_id.get(int(mid)) if mid else None
    ) or conso_by_lib.get(_norm(lib), {})

    conso_qty = float(conso_info.get("quantite", 0))

    # Filtrer
    if masquer_sans_conso and conso_qty == 0:
        continue
    # Ne garder que les MP qui apparaissent dans la synthèse (ou pas d'ingrédients)
    has_conso = mid in conso_ids or _norm(lib) in conso_by_lib
    if not has_conso and not _is_packaging(mp):
        continue

    conso_jour = conso_qty / max(eb_window, 1)
    duree      = (qty_v / conso_jour) if conso_jour > 0 else float("inf")

    besoin_h   = conso_jour * horizon_j
    a_commander = max(0.0, besoin_h - qty_v)

    rows_comp.append({
        "Composant":       lib,
        "Type":            type_lib,
        "Stock actuel":    int(round(qty_v)),
        "Unité":           unite,
        f"Conso ({eb_window}j)": int(round(conso_qty)),
        "Conso/jour":      round(conso_jour, 1),
        "Durée (jours)":   int(round(duree)) if duree != float("inf") else 9999,
        "Statut":          _status_icon(duree),
        "Seuil bas":       int(round(seuil)) if seuil else 0,
        # colonnes cachées pour le calcul
        "_id":             mid,
        "_duree_raw":      duree,
        "_besoin_h":       round(besoin_h, 0),
        "_a_commander":    round(a_commander, 0),
    })

if rows_comp:
    df_comp = (
        pd.DataFrame(rows_comp)
        .sort_values("_duree_raw")
        .reset_index(drop=True)
    )

    # KPIs
    crit_c = int((df_comp["Durée (jours)"] < seuil_rouge).sum())
    att_c  = int(((df_comp["Durée (jours)"] >= seuil_rouge) & (df_comp["Durée (jours)"] < seuil_orange)).sum())
    ok_c   = int((df_comp["Durée (jours)"] >= seuil_orange).sum())

    c1, c2, c3 = st.columns(3)
    with c1: kpi(f"🔴 Critique (< {seuil_rouge}j)",   str(crit_c))
    with c2: kpi(f"🟡 Attention (< {seuil_orange}j)", str(att_c))
    with c3: kpi("🟢 OK",                             str(ok_c))

    # Table d'affichage (sans colonnes internes)
    display_cols = [
        "Statut", "Composant", "Type", "Stock actuel", "Unité",
        f"Conso ({eb_window}j)", "Conso/jour", "Durée (jours)", "Seuil bas"
    ]
    df_display = df_comp[display_cols].copy()
    df_display["Durée (jours)"] = df_display["Durée (jours)"].apply(
        lambda x: "∞" if x >= 9999 else str(x)
    )

    st.dataframe(
        df_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Stock actuel":              st.column_config.NumberColumn(format="%d"),
            f"Conso ({eb_window}j)":     st.column_config.NumberColumn(format="%d"),
            "Conso/jour":                st.column_config.NumberColumn(format="%.1f"),
            "Seuil bas":                 st.column_config.NumberColumn(format="%d"),
        }
    )

    # Expander : données brutes Easy Beer pour debug
    with st.expander("🔍 Voir les données brutes de consommation (Easy Beer)", expanded=False):
        raw_rows = []
        for elem in conso_elements:
            raw_rows.append({
                "id":      elem.get("idMatierePremiere"),
                "Libellé": elem.get("libelle"),
                "Qté":     elem.get("quantite"),
                "Unité":   elem.get("unite"),
                "Coût":    elem.get("cout"),
            })
        if raw_rows:
            st.dataframe(pd.DataFrame(raw_rows), use_container_width=True, hide_index=True)
        else:
            st.info("Aucune donnée de consommation reçue.")

else:
    st.info(
        "Aucun composant d'emballage trouvé. "
        "Vérifie que les matières premières de type **Conditionnement** "
        "sont configurées dans Easy Beer et utilisées dans les brassins."
    )

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — COMMANDE RECOMMANDÉE
# ═══════════════════════════════════════════════════════════════════════════════
section(f"🛒 Commande recommandée — horizon {horizon_j} j", "🛒")

if rows_comp:
    to_order = [
        {
            "Composant":              r["Composant"],
            "Unité":                  r["Unité"],
            f"Besoin ({horizon_j}j)": int(r["_besoin_h"]),
            "Stock actuel":           r["Stock actuel"],
            "À commander":            int(r["_a_commander"]),
            "Statut":                 r["Statut"],
        }
        for r in rows_comp
        if r["_a_commander"] > 0
    ]
    to_order.sort(key=lambda x: -x["À commander"])

    if to_order:
        df_order = pd.DataFrame(to_order)

        nb_cmd   = len(df_order)
        total_u  = int(df_order["À commander"].sum())

        c1, c2 = st.columns(2)
        with c1: kpi("Articles à commander", str(nb_cmd))
        with c2: kpi("Total unités à commander", f"{total_u:,}".replace(",", " "))

        st.dataframe(
            df_order,
            use_container_width=True,
            hide_index=True,
            column_config={
                f"Besoin ({horizon_j}j)": st.column_config.NumberColumn(format="%d"),
                "Stock actuel":           st.column_config.NumberColumn(format="%d"),
                "À commander":            st.column_config.NumberColumn(format="%d"),
            }
        )

        csv_bytes = df_order.to_csv(index=False).encode("utf-8")
        st.download_button(
            f"⬇️ Exporter la commande (CSV)",
            data=csv_bytes,
            file_name=f"commande_emballages_{horizon_j}j_{datetime.date.today()}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.success(f"✅ Aucune commande nécessaire sur {horizon_j} jours.")
else:
    st.info("Synchronise les données pour calculer les recommandations.")

# ─── Footer ────────────────────────────────────────────────────────────────────
user_menu_footer(user)
