# pages/01_Accueil.py
from __future__ import annotations

# 🔐 Auth / menu
from common.session import require_login, user_menu, user_menu_footer
user = require_login()
user_menu()

import streamlit as st
from common.design import apply_theme, section
from core.optimizer import read_input_excel_and_period_from_upload

# 🎨 titre / thème
apply_theme("Ferment Station — Accueil", "🥤")
section("Accueil", "🏠")
st.caption("Dépose ici ton fichier Excel. Il sera utilisé automatiquement dans tous les onglets.")

# 📤 upload
uploaded = st.file_uploader("Dépose un Excel (.xlsx / .xls)", type=["xlsx", "xls"])

col1, col2 = st.columns([1, 1])
with col1:
    clear = st.button("♻️ Réinitialiser le fichier chargé", use_container_width=True)
with col2:
    show_head = st.toggle("Afficher un aperçu (20 premières lignes)", value=True)

# 🔄 reset
if clear:
    for k in ("df_raw", "window_days", "file_name"):
        st.session_state.pop(k, None)
    st.success("Fichier déchargé. Dépose un nouvel Excel pour continuer.")

# ✅ traitement du fichier
if uploaded is not None:
    try:
        df_raw, window_days = read_input_excel_and_period_from_upload(uploaded)
        st.session_state.df_raw = df_raw
        st.session_state.window_days = window_days
        st.session_state.file_name = uploaded.name
        st.success(
            f"Fichier chargé ✅ : **{uploaded.name}** · Fenêtre détectée (B2) : **{window_days} jours**"
        )
    except Exception as e:
        st.error(f"Erreur de lecture de l'Excel : {e}")

# 🟣 état courant
if "df_raw" in st.session_state:
    st.info(
        f"Fichier en mémoire : **{st.session_state.get('file_name','(sans nom)')}** — "
        f"fenêtre : **{st.session_state.get('window_days', '—')} jours**"
    )
    if show_head:
        st.dataframe(st.session_state.df_raw.head(20), use_container_width=True)
else:
    st.warning("Aucun fichier en mémoire. Dépose un Excel ci-dessus pour activer les autres onglets.")

# --- Footer sidebar (doit être le DERNIER appel de la page) ---
user_menu_footer(user)
