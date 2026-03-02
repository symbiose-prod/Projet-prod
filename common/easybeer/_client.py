"""
common/easybeer/_client.py
==========================
Shared HTTP client: config, auth, rate-limiter, error handling, payload builders.
"""
from __future__ import annotations

import datetime
import logging
import os
import threading as _threading
import time as _time
from typing import Any

import requests

_log = logging.getLogger("ferment.easybeer")

# ─── Config ──────────────────────────────────────────────────────────────────
BASE = "https://api.easybeer.fr"
TIMEOUT = 30  # secondes


def is_configured() -> bool:
    """True si les credentials Easy Beer sont presents."""
    return bool(os.environ.get("EASYBEER_API_USER") and os.environ.get("EASYBEER_API_PASS"))


# ─── Rate-limiter global (thread-safe) ───────────────────────────────────────
_API_MIN_INTERVAL = 0.2  # secondes
_api_last_ts: float = 0.0
_api_lock = _threading.Lock()


def _throttle() -> None:
    """Espace les appels API de min 200ms pour eviter le ban rate-limit (thread-safe)."""
    global _api_last_ts
    with _api_lock:
        now = _time.monotonic()
        wait = _API_MIN_INTERVAL - (now - _api_last_ts)
        if wait > 0:
            _time.sleep(wait)
        _api_last_ts = _time.monotonic()


def _auth() -> tuple[str, str]:
    _throttle()
    return (
        os.environ.get("EASYBEER_API_USER", ""),
        os.environ.get("EASYBEER_API_PASS", ""),
    )


class EasyBeerError(RuntimeError):
    """Erreur lors d'un appel a l'API EasyBeer."""


def _check_response(r: requests.Response, endpoint: str) -> None:
    """Verifie la reponse HTTP et leve une erreur lisible."""
    if r.ok:
        return
    body = r.text[:500]
    if "<!DOCTYPE" in body or "<html" in body.lower():
        raise EasyBeerError(
            f"EasyBeer {endpoint} \u2192 HTTP {r.status_code} : le serveur a renvoy\u00e9 une page HTML "
            f"(maintenance ou erreur proxy). R\u00e9essayez dans quelques minutes."
        )
    raise EasyBeerError(
        f"EasyBeer {endpoint} \u2192 HTTP {r.status_code} : {body[:300]}"
    )


def _dates(window_days: int) -> tuple[str, str]:
    """Retourne (date_debut_iso, date_fin_iso) pour une fenetre de N jours."""
    fin = datetime.datetime.now(datetime.timezone.utc)
    debut = fin - datetime.timedelta(days=window_days)
    return (
        debut.strftime("%Y-%m-%dT00:00:00.000Z"),
        fin.strftime("%Y-%m-%dT23:59:59.999Z"),
    )


def _base_payload(window_days: int) -> dict[str, Any]:
    """Payload commun pour tous les endpoints /indicateur/* et /export/excel."""
    debut, fin = _dates(window_days)
    return {
        "idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013")),
        "periode": {
            "dateDebut": debut,
            "dateFin": fin,
            "type": "PERIODE_LIBRE",
        },
    }


# Alias pour compatibilite interne
_excel_payload = _base_payload
_indicator_payload = _base_payload
