"""
pages/_admin_helpers.py
=======================
Helpers partagés entre les pages admin (préfixe ``_`` = autorisé à être
importé par d'autres pages selon test_architecture_layers).
"""
from __future__ import annotations

from nicegui import ui

from pages.auth import require_auth


def require_admin() -> dict | None:
    """Retourne le user si admin, sinon redirige /accueil et retourne None.

    Utilisable par n'importe quelle page sous /admin/* pour gater l'accès.
    """
    user = require_auth()
    if not user:
        return None
    if user.get("role") != "admin":
        ui.notify(
            "Accès refusé : privilèges administrateur requis.",
            type="negative", icon="lock",
        )
        ui.navigate.to("/accueil")
        return None
    return user
