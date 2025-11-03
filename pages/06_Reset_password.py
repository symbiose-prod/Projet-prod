# pages/06_Reset_password.py
from __future__ import annotations
import streamlit as st
from common.auth_reset import verify_token, consume_token_and_set_password

st.set_page_config(page_title="Réinitialiser le mot de passe", page_icon="🔒", initial_sidebar_state="collapsed")
st.markdown("""
<style>
section[data-testid="stSidebar"] {display:none !important;}
section[data-testid="stSidebarNav"] {display:none !important;}
</style>
""", unsafe_allow_html=True)

st.title("Réinitialiser le mot de passe")

query_params = st.experimental_get_query_params()  # st.query_params si tu es sur Streamlit >= 1.32
token_param = query_params.get("token")
token = token_param[0] if isinstance(token_param, list) else token_param

if not token:
    st.error("Lien invalide : jeton manquant.")
    st.stop()

checked = verify_token(token)
if not checked:
    st.error("Lien de réinitialisation invalide ou expiré (ou déjà utilisé).")
    st.stop()

with st.form("reset_form", clear_on_submit=False):
    st.write(f"Adresse : **{checked['email']}**")
    new_pwd = st.text_input("Nouveau mot de passe", type="password")
    new_pwd2 = st.text_input("Confirmer le mot de passe", type="password")
    submit = st.form_submit_button("Mettre à jour le mot de passe")

if submit:
    if not new_pwd or new_pwd != new_pwd2:
        st.error("Les deux mots de passe doivent être identiques.")
        st.stop()
    ok = consume_token_and_set_password(
        reset_id=checked["reset_id"],
        user_id=checked["user_id"],
        new_password=new_pwd
    )
    if ok:
        st.success("Mot de passe mis à jour. Vous pouvez maintenant vous connecter.")
        st.page_link("pages/00_Auth.py", label="Aller à la page de connexion →", icon="🔐")
