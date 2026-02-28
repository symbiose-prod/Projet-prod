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

# ─── Chargement .env (python-dotenv, ne surcharge pas les vars existantes) ───
from pathlib import Path
from dotenv import load_dotenv

_env_file = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_file, override=False)


# ─── Auth middleware ─────────────────────────────────────────────────────────

import logging as _logging

_log = _logging.getLogger("ferment.auth")

# Pages publiques (pas besoin d'etre connecte)
PUBLIC_PATHS = {"/login", "/_nicegui", "/favicon.ico", "/reset"}

# Cookie remember-me : duree par defaut (30 jours)
_REMEMBER_MAX_AGE = 30 * 86400


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # ── Logout endpoint : revoque token + clear cookie + redirect ──
        if path == "/api/logout":
            return self._handle_logout(request)

        # Laisser passer les assets NiceGUI et les pages publiques
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            response = await call_next(request)
            self._add_security_headers(response)
            return response

        # Verifier l'authentification cote storage
        user_store = app.storage.user
        if not user_store.get("authenticated"):
            # Tentative de restauration via cookie "Se souvenir de moi"
            fs_token = request.cookies.get("fs_session")
            if fs_token:
                try:
                    from common.auth import verify_session_token
                    remembered = verify_session_token(fs_token)
                    if remembered:
                        user_store.update({
                            "authenticated": True,
                            "id": remembered["id"],
                            "tenant_id": remembered["tenant_id"],
                            "email": remembered["email"],
                            "role": remembered["role"],
                        })
                        _log.info("Session restauree via remember-me pour %s", remembered["email"])
                    else:
                        return RedirectResponse(url="/login")
                except Exception:
                    _log.warning("Erreur verification remember-me token", exc_info=True)
                    return RedirectResponse(url="/login")
            else:
                return RedirectResponse(url="/login")

        # Validation serveur periodique (toutes les 5 min max)
        import time
        now = time.time()
        last_check = user_store.get("_server_validated_at", 0)
        if now - last_check > 300:  # 5 minutes
            try:
                from common.auth import find_user_by_email
                user_email = user_store.get("email", "")
                db_user = find_user_by_email(user_email) if user_email else None
                if not db_user or not db_user.get("is_active"):
                    _log.warning("Session invalidee : user %s introuvable ou desactive", user_email)
                    user_store.clear()
                    return RedirectResponse(url="/login")
                # Resync tenant_id (protection contre falsification cote client)
                user_store["tenant_id"] = str(db_user["tenant_id"])
                user_store["role"] = db_user.get("role", "user")
                user_store["_server_validated_at"] = now
            except Exception:
                _log.exception("Erreur validation session serveur")
                # Grace period : si la derniere validation reussie date de
                # moins de 30 min, on laisse passer temporairement.
                _GRACE_SECONDS = 1800  # 30 min
                if last_check == 0 or (now - last_check) > _GRACE_SECONDS:
                    _log.warning(
                        "Grace period expiree (DB down), deconnexion de %s",
                        user_store.get("email"),
                    )
                    user_store.clear()
                    return RedirectResponse(url="/login")

        # ── Process request ──
        response = await call_next(request)

        # ── Headers de sécurité ──
        self._add_security_headers(response)

        # ── Poser le cookie remember-me HttpOnly si pending ──
        try:
            pending_token = user_store.get("_pending_remember_token")
            if pending_token:
                try:
                    del user_store["_pending_remember_token"]
                except KeyError:
                    pass
                _is_prod = os.environ.get("ENV") == "production"
                response.set_cookie(
                    "fs_session",
                    pending_token,
                    max_age=_REMEMBER_MAX_AGE,
                    path="/",
                    httponly=True,
                    secure=_is_prod,
                    samesite="lax",
                )
        except Exception:
            _log.warning("Erreur pose cookie remember-me", exc_info=True)

        return response

    @staticmethod
    def _add_security_headers(response) -> None:
        """Ajoute les headers de sécurité sur toutes les réponses (publiques et authentifiées)."""
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if os.environ.get("ENV") == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    @staticmethod
    def _handle_logout(request: Request) -> RedirectResponse:
        """Logout: revoque le token DB + vide la session NiceGUI + supprime le cookie."""
        fs_token = request.cookies.get("fs_session")
        if fs_token:
            try:
                from common.auth import revoke_session_token
                revoke_session_token(fs_token)
            except Exception:
                _log.warning("Erreur revocation token logout", exc_info=True)
        # Vider le storage NiceGUI (supprime authenticated, tenant_id, etc.)
        try:
            app.storage.user.clear()
        except Exception:
            _log.debug("Impossible de vider storage user au logout", exc_info=True)
        resp = RedirectResponse(url="/login", status_code=302)
        resp.delete_cookie("fs_session", path="/")
        return resp


app.add_middleware(AuthMiddleware)

# ─── Import des pages (les @ui.page sont enregistrés à l'import) ────────────

from ui import auth as _auth           # /login
from ui import accueil as _accueil     # /accueil
from ui import ramasse as _ramasse     # /ramasse
from ui import production as _production  # /production


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
