# common/error_alerting.py — Alerte email sur erreurs 500
"""
Envoie un email d'alerte quand une erreur 500 se produit en production.

Anti-flood : maximum 1 email par tranche de 5 minutes pour éviter
de saturer la boîte en cas de boucle d'erreurs.
"""
from __future__ import annotations

import logging
import os
import threading
import time

_log = logging.getLogger("ferment.alerting")

# ─── Anti-flood ──────────────────────────────────────────────────────────────
_COOLDOWN_SECONDS = 300  # 5 minutes entre chaque alerte
_last_alert_ts: float = 0.0
_lock = threading.Lock()

# Destinataire des alertes (toi)
_ALERT_RECIPIENT = "nicolas@symbiose-kefir.fr"


def _should_send() -> bool:
    """Retourne True si le cooldown est écoulé (thread-safe)."""
    global _last_alert_ts
    now = time.monotonic()
    with _lock:
        if now - _last_alert_ts < _COOLDOWN_SECONDS:
            return False
        _last_alert_ts = now
        return True


def send_error_alert(
    *,
    method: str,
    path: str,
    status_code: int,
    request_id: str,
    user_email: str | None = None,
    error_detail: str | None = None,
) -> None:
    """Envoie une alerte email pour une erreur serveur (fire-and-forget dans un thread)."""
    if os.environ.get("ENV") not in ("production", "staging"):
        return
    if not _should_send():
        _log.debug("Alerte 500 supprimée (cooldown actif)")
        return

    def _send():
        try:
            from common.email import _post_brevo, _require_env

            api_key, sender_email, sender_name = _require_env()
            env_name = os.environ.get("ENV", "unknown").upper()
            subject = f"[{env_name}] Erreur {status_code} - {method} {path}"

            body_lines = [
                f"<h2>Erreur {status_code} sur Ferment Station</h2>",
                f"<p><strong>Environnement :</strong> {env_name}</p>",
                f"<p><strong>Request ID :</strong> <code>{request_id}</code></p>",
                f"<p><strong>Endpoint :</strong> <code>{method} {path}</code></p>",
            ]
            if user_email:
                body_lines.append(f"<p><strong>Utilisateur :</strong> {user_email}</p>")
            if error_detail:
                body_lines.append(
                    "<p><strong>Détail :</strong></p>"
                    "<pre style='background:#f5f5f5;padding:12px;border-radius:4px;"
                    f"font-size:13px;overflow-x:auto'>{error_detail}</pre>"
                )
            body_lines.append(
                "<hr><p style='color:#888;font-size:12px'>"
                "Alerte automatique Ferment Station — max 1 email / 5 min</p>"
            )

            _post_brevo("/v3/smtp/email", {
                "sender": {"email": sender_email, "name": sender_name},
                "to": [{"email": _ALERT_RECIPIENT}],
                "subject": subject,
                "htmlContent": "\n".join(body_lines),
            })
            _log.info("Alerte erreur %d envoyée pour %s %s", status_code, method, path)
        except Exception:
            _log.warning("Impossible d'envoyer l'alerte erreur", exc_info=True)

    threading.Thread(target=_send, daemon=True, name="error-alert").start()
