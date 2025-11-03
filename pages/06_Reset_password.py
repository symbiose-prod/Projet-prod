# pages/06_Reset_password.py
from __future__ import annotations
import streamlit as st

st.set_page_config(page_title="Réinitialisation du mot de passe", page_icon="🔑")

# ⚠️ page publique : pas de require_login, pas de user_menu
from common.auth_reset import verify_reset_token, consume_token_and_set_password

st.title("🔑 Réinitialiser le mot de passe")

# 1) récupérer le token dans l’URL
query_params = st.query_params
raw_token = query_params.get("token")
if isinstance(raw_token, list):
    token = raw_token[0]
else:
    token = raw_token

if not token:
    st.error("Lien invalide : aucun token fourni.")
    st.stop()

# 2) vérifier le token en base
ok, info = verify_reset_token(token)
if not ok:
    st.error(info or "Lien de réinitialisation invalide ou expiré.")
    st.stop()

user_id = info["user_id"]
reset_id = info["reset_id"]

st.success("Lien valide ✅. Choisissez un nouveau mot de passe.")

pwd1 = st.text_input("Nouveau mot de passe", type="password")
pwd2 = st.text_input("Confirmez le mot de passe", type="password")

if st.button("Changer le mot de passe", type="primary"):
    if not pwd1 or not pwd2:
        st.error("Veuillez saisir et confirmer le mot de passe.")
    elif pwd1 != pwd2:
        st.error("Les mots de passe ne correspondent pas.")
    else:
        try:
            consume_token_and_set_password(reset_id, user_id, pwd1)
            st.success("Mot de passe mis à jour ✅")
            st.info("Vous pouvez maintenant vous connecter depuis la page d’authentification.")
            st.page_link("pages/00_Auth.py", label="➡️ Aller à la page de connexion")
        except Exception as e:
            st.error(f"Erreur lors de la mise à jour : {e}")
