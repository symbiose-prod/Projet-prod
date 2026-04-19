"""
common/ramasse_draft.py
=======================
Auto-save des fiches de ramasse en cours de saisie.

Protège contre la perte de travail sur crash/fermeture accidentelle d'onglet.
Stockage dans ``app.storage.user`` (persistant par session utilisateur, chiffré
via ``NICEGUI_SECRET``).

Un seul brouillon courant à la fois par utilisateur — suffit pour l'usage
opérationnel (une fiche de ramasse en cours à la fois).

Model du brouillon::

    {
        "date_iso": "2026-04-19",
        "destinataire": "SOFRIPA Lyon",
        "brassin_ids": [123, 456],
        "cartons": {"REF001": 5, "REF002": 3},
        "palettes": {"REF001": 2},   # overrides uniquement
        "packaging": {"Palette bois": 1},
        "saved_at": 1713513600,      # unix ts monotonic→epoch via time.time()
    }
"""
from __future__ import annotations

import logging
import time
from typing import Any

_log = logging.getLogger("ferment.ramasse_draft")

_KEY = "ramasse_draft"

# Au-delà de 24h le brouillon est considéré périmé (l'utilisateur a probablement
# changé de contexte ; éviter de proposer un brouillon obsolète).
_MAX_AGE_SECONDS = 24 * 3600


def _storage() -> dict | None:
    """Retourne le dict app.storage.user, ou None si pas de session active."""
    try:
        from nicegui import app
        return app.storage.user
    except Exception:
        _log.debug("Pas de session NiceGUI pour le brouillon", exc_info=True)
        return None


def save_draft(
    *,
    date_iso: str,
    destinataire: str,
    brassin_ids: list[int] | None,
    cartons: dict[str, int],
    palettes: dict[str, int] | None = None,
    packaging: dict[str, int] | None = None,
) -> None:
    """Écrit (ou écrase) le brouillon courant. Fire-and-forget (ne lève rien)."""
    store = _storage()
    if store is None:
        return
    # Ne persiste que s'il y a au moins une saisie non-vide — évite de créer
    # des brouillons inutiles dès l'ouverture de la page.
    if not cartons and not palettes and not packaging:
        return
    try:
        store[_KEY] = {
            "date_iso": date_iso,
            "destinataire": destinataire,
            "brassin_ids": list(brassin_ids or []),
            "cartons": {k: int(v) for k, v in (cartons or {}).items() if int(v or 0) > 0},
            "palettes": {k: int(v) for k, v in (palettes or {}).items() if int(v or 0) > 0},
            "packaging": {k: int(v) for k, v in (packaging or {}).items() if int(v or 0) > 0},
            "saved_at": int(time.time()),
        }
    except Exception:
        _log.debug("Écriture brouillon échouée", exc_info=True)


def load_draft() -> dict[str, Any] | None:
    """Charge le brouillon s'il existe et n'est pas périmé. Sinon None."""
    store = _storage()
    if store is None:
        return None
    draft = store.get(_KEY)
    if not isinstance(draft, dict):
        return None
    saved_at = int(draft.get("saved_at") or 0)
    if time.time() - saved_at > _MAX_AGE_SECONDS:
        # Périmé — nettoyage silencieux
        clear_draft()
        return None
    return draft


def clear_draft() -> None:
    """Supprime le brouillon courant (appelé après sauvegarde réussie)."""
    store = _storage()
    if store is None:
        return
    try:
        store.pop(_KEY, None)
    except Exception:
        _log.debug("Suppression brouillon échouée", exc_info=True)


def draft_age_human(draft: dict[str, Any]) -> str:
    """Retourne l'âge du brouillon en FR lisible (ex: 'il y a 3 min')."""
    saved_at = int(draft.get("saved_at") or 0)
    if saved_at <= 0:
        return "à l'instant"
    age = int(time.time()) - saved_at
    if age < 60:
        return "il y a quelques secondes"
    if age < 3600:
        mins = age // 60
        return f"il y a {mins} min"
    if age < 86400:
        hrs = age // 3600
        return f"il y a {hrs}h"
    return "il y a plus de 24h"
