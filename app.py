from __future__ import annotations
import streamlit as st
from common.session import is_authenticated

# ---- réception d'un token de reset depuis l'URL racine ----
qp = st.query_params
raw_token = qp.get("token")
if isinstance(raw_token, list):
    token = raw_token[0]
else:
    token = raw_token

if token:
    # on le met en session pour la page d'auth
    st.session_state["reset_token_from_link"] = token
    # et on bascule vers la page d'auth
    st.switch_page("pages/00_Auth.py")

# si l'URL contient le token, on bascule automatiquement vers la page Auth
_qp = st.query_params
if "reset_token" in _qp:
    # on laisse le token dans l'URL et on va sur la page d'auth
    st.switch_page("pages/_00_Auth.py")

st.set_page_config(page_title="Accueil", page_icon="🏠", initial_sidebar_state="collapsed")

# Si l'utilisateur n'est pas connecté → on l'envoie sur la page d'auth
if not is_authenticated():
    st.switch_page("pages/00_Auth.py")

# Si connecté → on redirige vers la page principale de travail
st.switch_page("pages/01_Accueil.py")
