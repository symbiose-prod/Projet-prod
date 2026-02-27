#!/usr/bin/env python3
"""
app_nicegui.py
==============
Point d'entrée NiceGUI — Ferment Station.

Lance avec :  python3 app_nicegui.py
"""
from __future__ import annotations

import os

from nicegui import ui, app
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

# ─── Chargement .env (local dev) ────────────────────────────────────────────
from pathlib import Path

_env_file = Path(__file__).resolve().parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


# ─── Auth middleware ─────────────────────────────────────────────────────────

import logging as _logging

_log = _logging.getLogger("ferment.auth")

# Pages publiques (pas besoin d'être connecté)
PUBLIC_PATHS = {"/login", "/_nicegui", "/favicon.ico"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Laisser passer les assets NiceGUI et les pages publiques
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)

        # Vérifier l'authentification côté storage
        user_store = app.storage.user
        if not user_store.get("authenticated"):
            return RedirectResponse(url="/login")

        # Validation serveur périodique (toutes les 5 min max)
        import time
        now = time.time()
        last_check = user_store.get("_server_validated_at", 0)
        if now - last_check > 300:  # 5 minutes
            try:
                from common.auth import find_user_by_email
                user_email = user_store.get("email", "")
                db_user = find_user_by_email(user_email) if user_email else None
                if not db_user or not db_user.get("is_active"):
                    _log.warning("Session invalidée : user %s introuvable ou désactivé", user_email)
                    user_store.clear()
                    return RedirectResponse(url="/login")
                # Resync tenant_id (protection contre falsification côté client)
                user_store["tenant_id"] = str(db_user["tenant_id"])
                user_store["role"] = db_user.get("role", "user")
                user_store["_server_validated_at"] = now
            except Exception:
                _log.exception("Erreur validation session serveur")
                # Grace period : si la dernière validation réussie date de
                # moins de 30 min, on laisse passer temporairement.
                # Au-delà, fail-closed → déconnexion (la DB est down trop longtemps).
                _GRACE_SECONDS = 1800  # 30 min
                if last_check == 0 or (now - last_check) > _GRACE_SECONDS:
                    _log.warning(
                        "Grace period expirée (DB down), déconnexion de %s",
                        user_store.get("email"),
                    )
                    user_store.clear()
                    return RedirectResponse(url="/login")

        return await call_next(request)


app.add_middleware(AuthMiddleware)

# ─── Import des pages (les @ui.page sont enregistrés à l'import) ────────────

from ui import auth as _auth           # /login
from ui import accueil as _accueil     # /accueil
from ui import ramasse as _ramasse     # /ramasse
from ui import production as _production  # /production
from ui import achats as _achats       # /achats


# ─── Nettoyage périodique (sessions / resets expirés) ────────────────────────

@app.on_startup
async def _startup_cleanup():
    """Nettoie les sessions et tokens expirés au démarrage."""
    try:
        from common.auth import cleanup_expired_sessions, cleanup_expired_resets
        cleanup_expired_sessions()
        cleanup_expired_resets()
    except Exception:
        _log.exception("Erreur nettoyage au démarrage")


# ─── Redirect racine ────────────────────────────────────────────────────────

@ui.page("/")
def root():
    ui.navigate.to("/accueil")


# ─── Lancement ──────────────────────────────────────────────────────────────

def _get_storage_secret() -> str:
    """Exige un vrai secret pour signer les cookies de session."""
    secret = os.environ.get("NICEGUI_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "NICEGUI_SECRET manquant — génère-en un :\n"
            '  python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return secret


if __name__ in {"__main__", "__mp_main__"}:
    port = int(os.environ.get("NICEGUI_PORT", "8502"))
    ui.run(
        title="Ferment Station",
        port=port,
        show=False,
        reload=os.environ.get("ENV") != "production",
        favicon="🧪",
        dark=False,
        language="fr",
        storage_secret=_get_storage_secret(),
    )
