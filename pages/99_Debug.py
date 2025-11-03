# pages/99_Debug.py
import pathlib
import traceback
import streamlit as st

# ⚠️ Toujours configurer la page AVANT toute autre commande Streamlit
st.set_page_config(page_title="Debug pages", page_icon="🛠️", layout="wide")

# On utilise notre fabrique de connexion unique
from db.conn import run_sql, debug_dsn, whoami

st.title("🛠️ Debug des pages Streamlit")

# --- Section debug DB -------------------------------------------------------
st.subheader("Test de connexion à la base de données")
try:
    row = run_sql("SELECT now() AS server_time;").mappings().first()
    st.success(f"✅ Connexion DB OK — serveur : {row['server_time']}")
except Exception as e:
    st.error(f"❌ Connexion DB KO : {e}")

# Infos utiles (sans secrets)
st.caption(f"DB debug: {debug_dsn()}")
st.caption(f"DB user (via conn.py): {whoami()}")

st.divider()
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
        st.error(f"SYNTAX ERROR dans {p.name} — ligne {e.lineno}, colonne {e.offset}")
        st.code("".join(traceback.format_exception_only(e)), language="text")
        # Montre la ligne incriminée
        lines = code.splitlines()
        i = max(0, (e.lineno or 1) - 1)
        snippet = "\n".join(lines[max(0, i-2): i+3])
        st.code(snippet, language="python")
        bad.append(p.name)

if not bad:
    st.info("✅ Toutes les pages compilent correctement.")
else:
    st.warning("Corrige les pages en erreur ci-dessus puis rafraîchis.")


# pages/99_Debug.py
from __future__ import annotations
import os, streamlit as st
from db.conn import run_sql
from common.auth_reset import create_password_reset

st.title("Debug")
st.write("Ping DB…")
try:
    r = run_sql("SELECT now() AS now")
    st.success(f"DB OK — {r[0]['now']}")
except Exception as e:
    st.error(f"DB KO — {e}")

st.divider()
email = st.text_input("Email pour fake reset URL")
if st.button("Générer un lien (si user existe)"):
    url = create_password_reset(email, request_ip="debug", request_ua="debug")
    st.write(url or "(aucun — throttling ou email inconnu)")

st.divider()
rows = run_sql("SELECT id, user_id, expires_at, used_at, created_at FROM password_resets ORDER BY id DESC LIMIT 5")
st.write(rows)

