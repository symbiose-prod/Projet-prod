# pages/01_Accueil.py
from __future__ import annotations

# 🔐 Auth / menu
from common.session import require_login, user_menu, user_menu_footer
user = require_login()
user_menu()

import datetime
import os
import requests
import streamlit as st
from common.design import apply_theme, section
from core.optimizer import read_input_excel_and_period_from_upload, read_input_excel_and_period_from_bytes

# 🎨 titre / thème
apply_theme("Ferment Station — Accueil", "🥤")
section("Accueil", "🏠")

# ─── Configuration Easy Beer (variables d'environnement) ───────────────────────
EASYBEER_API_USER = os.environ.get("EASYBEER_API_USER", "")
EASYBEER_API_PASS = os.environ.get("EASYBEER_API_PASS", "")
EASYBEER_ID_BRASSERIE = int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013"))
EASYBEER_WINDOW_DAYS = int(os.environ.get("EASYBEER_WINDOW_DAYS", "30"))

def sync_easybeer(window_days: int = EASYBEER_WINDOW_DAYS):
    """Appelle l'API Easy Beer et retourne les bytes du fichier Excel autonomie-stocks."""
    date_fin = datetime.datetime.utcnow()
    date_debut = date_fin - datetime.timedelta(days=window_days)
    payload = {
        "idBrasserie": EASYBEER_ID_BRASSERIE,
        "periode": {
            "dateDebut": date_debut.strftime("%Y-%m-%dT00:00:00.000Z"),
            "dateFin": date_fin.strftime("%Y-%m-%dT23:59:59.999Z"),
        }
    }
    resp = requests.post(
        "https://api.easybeer.fr/indicateur/autonomie-stocks/export/excel",
        json=payload,
        auth=(EASYBEER_API_USER, EASYBEER_API_PASS),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content

# ─── Section Easy Beer ─────────────────────────────────────────────────────────
st.subheader("🔄 Synchronisation Easy Beer")

easybeer_ok = bool(EASYBEER_API_USER and EASYBEER_API_PASS)

if not easybeer_ok:
    st.warning("Clés API Easy Beer non configurées. Configure `EASYBEER_API_USER` et `EASYBEER_API_PASS` dans les variables d'environnement.")
else:
    col_sync, col_days = st.columns([2, 1])
    with col_days:
        window = st.number_input("Période (jours)", min_value=7, max_value=365, value=EASYBEER_WINDOW_DAYS, step=1)
    with col_sync:
        st.write("")  # alignement vertical
        sync_btn = st.button("🔄 Importer depuis Easy Beer", use_container_width=True, type="primary")

    if sync_btn:
        with st.spinner("Connexion à Easy Beer en cours…"):
            try:
                excel_bytes = sync_easybeer(window_days=window)
                df_raw, window_days_detected = read_input_excel_and_period_from_bytes(excel_bytes)
                st.session_state.df_raw = df_raw
                st.session_state.window_days = window
                st.session_state.file_name = f"easybeer-autonomie-{datetime.date.today()}.xlsx"
                st.success(f"✅ Données Easy Beer importées ({window} jours) — {len(df_raw)} lignes chargées.")
            except requests.HTTPError as e:
                st.error(f"Erreur API Easy Beer : {e.response.status_code} — {e.response.text[:200]}")
            except Exception as e:
                st.error(f"Erreur lors de la synchronisation : {e}")

st.divider()

# ─── Upload manuel (fallback) ──────────────────────────────────────────────────
st.subheader("📤 Import manuel")
st.caption("Ou dépose directement ton fichier Excel autonomie-stocks exporté depuis Easy Beer.")

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

# ✅ traitement du fichier uploadé manuellement
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
    st.warning("Aucun fichier en mémoire. Synchronise depuis Easy Beer ou dépose un Excel ci-dessus.")

# --- Footer sidebar (doit être le DERNIER appel de la page) ---
user_menu_footer(user)
