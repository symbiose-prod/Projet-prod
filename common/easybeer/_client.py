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
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

_log = logging.getLogger("ferment.easybeer")

# ─── Config ──────────────────────────────────────────────────────────────────
BASE = "https://api.easybeer.fr"
TIMEOUT = 30  # secondes


def is_configured() -> bool:
    """True si les credentials Easy Beer sont presents."""
    return bool(os.environ.get("EASYBEER_API_USER") and os.environ.get("EASYBEER_API_PASS"))


# ─── Session réutilisable (connection pooling + keep-alive) ──────────────────
_session: requests.Session | None = None
_session_lock = _threading.Lock()


def get_session() -> requests.Session:
    """Singleton requests.Session thread-safe avec connection pooling."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=4,
                    pool_maxsize=8,
                    max_retries=0,  # retries gérés par tenacity
                )
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _session = s
    return _session


# ─── Rate-limiter global (thread-safe) ───────────────────────────────────────
_API_MIN_INTERVAL = 0.2  # secondes (5 req/s max nominal)
_api_last_ts: float = 0.0
_api_lock = _threading.Lock()
_api_backoff_until: float = 0.0  # monotonic timestamp until which we enforce a cooldown


def _throttle() -> None:
    """Espace les appels API de min 200ms pour eviter le ban rate-limit (thread-safe).

    Si l'IP est bannie (backoff > 10s restant), lève une erreur immédiatement
    au lieu de bloquer le serveur pendant 5 minutes.
    """
    global _api_last_ts
    with _api_lock:
        now = _time.monotonic()
        remaining = _api_backoff_until - now
        if remaining > 10:
            # Banni pour longtemps — échouer immédiatement
            raise EasyBeerError(
                f"EasyBeer rate-limit actif — réessayez dans {int(remaining)}s"
            )
        if remaining > 0:
            # Petit cooldown — attendre
            _time.sleep(remaining)
            now = _time.monotonic()
        wait = _API_MIN_INTERVAL - (now - _api_last_ts)
        if wait > 0:
            _time.sleep(wait)
        _api_last_ts = _time.monotonic()


def _on_rate_limited(ban_seconds: float = 5.0) -> None:
    """Called when a rate-limit response is detected; enforces cooldown matching ban duration."""
    global _api_backoff_until
    # Minimum 5s, cap at 600s to avoid absurd waits
    cooldown = max(5.0, min(ban_seconds, 600.0))
    with _api_lock:
        new_until = _time.monotonic() + cooldown
        # Only extend, never shorten an existing backoff
        if new_until > _api_backoff_until:
            _api_backoff_until = new_until
    _log.warning("Rate-limit détecté — pause %.0fs avant prochains appels API", cooldown)


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
    # Rate-limit: HTTP 400 with "limit" / "banned" or HTTP 429
    if r.status_code in (429, 400) and any(
        kw in body.lower() for kw in ("limit", "banned", "rate")
    ):
        # Parse "Try again in X seconds" from response
        import re
        m = re.search(r"[Tt]ry again in (\d+)", body)
        ban_secs = float(m.group(1)) if m else 5.0
        _on_rate_limited(ban_secs)
    if "<!DOCTYPE" in body or "<html" in body.lower():
        raise EasyBeerError(
            f"EasyBeer {endpoint} \u2192 HTTP {r.status_code} : le serveur a renvoy\u00e9 une page HTML "
            f"(maintenance ou erreur proxy). R\u00e9essayez dans quelques minutes."
        )
    raise EasyBeerError(
        f"EasyBeer {endpoint} \u2192 HTTP {r.status_code} : {body[:300]}"
    )


def _safe_json(r: requests.Response, endpoint: str) -> Any:
    """Parse JSON en toute sécurité. Lève EasyBeerError si le body n'est pas du JSON."""
    try:
        return r.json()
    except (ValueError, TypeError) as exc:
        raise EasyBeerError(
            f"EasyBeer {endpoint} : réponse non-JSON (HTTP {r.status_code}) — {r.text[:200]}"
        ) from exc


def _dates(window_days: int) -> tuple[str, str]:
    """Retourne (date_debut_iso, date_fin_iso) pour une fenetre de N jours."""
    fin = datetime.datetime.now(datetime.UTC)
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


# ─── Retry decorator for transient API errors ───────────────────────────────

def _is_retryable(exc: BaseException) -> bool:
    """Return True for transient network/server errors worth retrying.

    Rate-limit errors (429 / 400+banned) are NOT retried by tenacity:
    the global _throttle() will block until the ban expires, so the
    *next user action* will succeed without wasting retry attempts.
    """
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code in (500, 502, 503, 504):
            return True
    if isinstance(exc, EasyBeerError):
        msg = str(exc)
        # Don't retry rate-limit: ban lasts 300s, retrying in 1-10s is pointless
        if any(kw in msg.lower() for kw in ("limit", "banned", "rate")):
            return False
        if any(f" {c}" in msg for c in ("500", "502", "503", "504")):
            return True
    return False


retry_api = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(_is_retryable),
    before_sleep=before_sleep_log(_log, logging.WARNING),
    reraise=True,
)
