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

# Pages publiques (pas besoin d'être connecté)
PUBLIC_PATHS = {"/login", "/_nicegui", "/favicon.ico"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Laisser passer les assets NiceGUI et les pages publiques
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)

        # Vérifier l'authentification
        if not app.storage.user.get("authenticated"):
            return RedirectResponse(url="/login")

        return await call_next(request)


app.add_middleware(AuthMiddleware)

# ─── Import des pages (les @ui.page sont enregistrés à l'import) ────────────

from ui import auth as _auth           # /login
from ui import accueil as _accueil     # /accueil
from ui import ramasse as _ramasse     # /ramasse
from ui import production as _production  # /production
from ui import achats as _achats       # /achats


# ─── Redirect racine ────────────────────────────────────────────────────────

@ui.page("/")
def root():
    ui.navigate.to("/accueil")


# ─── Lancement ──────────────────────────────────────────────────────────────

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
        storage_secret=os.environ.get("NICEGUI_SECRET", "ferment-station-dev-secret"),
    )
