# pages/99_Debug.py
from __future__ import annotations
import pathlib
import traceback
import streamlit as st

# ⚠️ Toujours configurer la page AVANT toute autre commande Streamlit
st.set_page_config(page_title="Debug pages", page_icon="🛠️", layout="wide")

# Connexion DB (run_sql renvoie list[dict] pour SELECT)
from db.conn import run_sql

# Optionnels : présents chez toi, mais on protège si absents
try:
    from db.conn import debug_dsn, whoami  # type: ignore
except Exception:
    debug_dsn = None
    whoami = None

st.title("🛠️ Debug des pages Streamlit")

# ---------------------------------------------------------------------------
# 1) Test de connexion à la base de données
# ---------------------------------------------------------------------------
st.subheader("Test de connexion à la base de données")
try:
    rows = run_sql("SELECT now() AS server_time;")
    server_time = rows[0]["server_time"] if rows else "(inconnu)"
    st.success(f"✅ Connexion DB OK — serveur : {server_time}")
except Exception as e:
    st.error(f"❌ Connexion DB KO : {e}")

# Infos utiles (sans secrets)
if callable(debug_dsn):
    try:
        st.caption(f"DB debug: {debug_dsn()}")
    except Exception:
        pass
if callable(whoami):
    try:
        st.caption(f"DB user (via conn.py): {whoami()}")
    except Exception:
        pass

st.divider()

# ---------------------------------------------------------------------------
# 2) Compilation des pages Streamlit (détecte les erreurs de syntaxe)
# ---------------------------------------------------------------------------
st.subheader("Compilation des pages Streamlit")
root = pathlib.Path(__file__).resolve().parents[1]  # racine du projet
pages = sorted((root / "pages").glob("*.py"))

bad = []
for p in pages:
    code = p.read_text(encoding="utf-8", errors="replace")
    try:
        compile(code, str(p), "exec")
        st.success(f"OK: {p.name}")
    except SyntaxError as e:
        st.error(f"SYNTAX ERROR dans {p.name} — ligne {getattr(e, 'lineno', '?')}, colonne {getattr(e, 'offset', '?')}")
        st.code("".join(traceback.format_exception_only(e)), language="text")
        lines = code.splitlines()
        i = max(0, (getattr(e, "lineno", 1) or 1) - 1)
        snippet = "\n".join(lines[max(0, i - 2): i + 3])
        st.code(snippet, language="python")
        bad.append(p.name)

if not bad:
    st.info("✅ Toutes les pages compilent correctement.")
else:
    st.warning("Corrige les pages en erreur ci-dessus puis rafraîchis.")

st.divider()

# ---------------------------------------------------------------------------
# 3) Outils de reset mot de passe (création de lien + listing derniers resets)
# ---------------------------------------------------------------------------
st.subheader("Réinitialisation de mot de passe — outils")

from common.auth_reset import create_password_reset  # import ici pour éviter les cycles au tout début

col1, col2 = st.columns([1, 2])

with col1:
    email = st.text_input("Email pour générer un lien de reset")
    if st.button("Générer un lien (si utilisateur existe)"):
        try:
            url = create_password_reset(email, request_ip="debug", request_ua="debug")
            st.success(url or "(aucun — throttling ou email inconnu)")
        except Exception as e:
            st.error(f"Erreur: {e}")

with col2:
    st.caption("Dernières entrées de password_resets (limité à 5)")
    try:
        rows = run_sql("""
            SELECT id, user_id, expires_at, used_at, created_at
            FROM password_resets
            ORDER BY id DESC
            LIMIT 5
        """)
        st.write(rows)
    except Exception as e:
        st.error(f"Erreur: {e}")
