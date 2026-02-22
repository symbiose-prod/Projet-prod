# pages/01_Accueil.py
from __future__ import annotations

# 🔐 Auth / menu
from common.session import require_login, user_menu, user_menu_footer
user = require_login()
user_menu()

import datetime
import os
import streamlit as st
from common.design import apply_theme, section
from core.optimizer import read_input_excel_and_period_from_upload, read_input_excel_and_period_from_bytes
import common.easybeer as eb

# 🎨 titre / thème
apply_theme("Ferment Station — Accueil", "🥤")
section("Accueil", "🏠")

# ─── Configuration Easy Beer ────────────────────────────────────────────────────
EASYBEER_WINDOW_DAYS = int(os.environ.get("EASYBEER_WINDOW_DAYS", "30"))

# ─── Layout deux colonnes ──────────────────────────────────────────────────────
col_left, col_right = st.columns(2, gap="large")

# ── Colonne gauche : Easy Beer ─────────────────────────────────────────────────
with col_left:
    st.subheader("🔄 Synchronisation Easy Beer")

    if not eb.is_configured():
        st.warning("Clés API Easy Beer non configurées.")
    else:
        window = st.number_input(
            "Période (jours)", min_value=7, max_value=365,
            value=EASYBEER_WINDOW_DAYS, step=1
        )
        sync_btn = st.button(
            "🔄 Importer depuis Easy Beer",
            use_container_width=True,
            type="primary"
        )

        if sync_btn:
            with st.spinner("Connexion à Easy Beer…"):
                try:
                    excel_bytes = eb.get_autonomie_stocks_excel(window_days=window)
                    df_raw, _ = read_input_excel_and_period_from_bytes(excel_bytes)
                    st.session_state.df_raw = df_raw
                    st.session_state.window_days = window
                    st.session_state.file_name = f"easybeer-autonomie-{datetime.date.today()}.xlsx"
                    st.success(f"✅ {len(df_raw)} lignes importées ({window} jours).")
                except requests.HTTPError as e:
                    st.error(f"Erreur API Easy Beer : {e.response.status_code}")
                except Exception as e:
                    st.error(f"Erreur : {e}")

# ── Colonne droite : Upload manuel ─────────────────────────────────────────────
with col_right:
    st.subheader("📤 Import manuel")
    st.caption("Fichier Excel exporté depuis Easy Beer.")

    uploaded = st.file_uploader(
        "Dépose un Excel (.xlsx / .xls)",
        type=["xlsx", "xls"],
        label_visibility="collapsed"
    )

    if uploaded is not None:
        try:
            df_raw, window_days = read_input_excel_and_period_from_upload(uploaded)
            st.session_state.df_raw = df_raw
            st.session_state.window_days = window_days
            st.session_state.file_name = uploaded.name
            st.success(f"✅ **{uploaded.name}** chargé · {window_days} jours.")
        except Exception as e:
            st.error(f"Erreur de lecture : {e}")

# ─── État courant + aperçu ─────────────────────────────────────────────────────
st.divider()

col_info, col_actions = st.columns([3, 1])
with col_info:
    if "df_raw" in st.session_state:
        st.info(
            f"📂 **{st.session_state.get('file_name', '(sans nom)')}** — "
            f"fenêtre : **{st.session_state.get('window_days', '—')} jours**"
        )
    else:
        st.warning("Aucun fichier en mémoire. Synchronise ou dépose un Excel.")

with col_actions:
    show_head = st.toggle("Aperçu", value=True)
    if st.button("♻️ Réinitialiser", use_container_width=True):
        for k in ("df_raw", "window_days", "file_name"):
            st.session_state.pop(k, None)
        st.success("Fichier déchargé.")
        st.rerun()

if "df_raw" in st.session_state and show_head:
    st.dataframe(st.session_state.df_raw.head(20), use_container_width=True)

# --- Footer sidebar ---
user_menu_footer(user)
