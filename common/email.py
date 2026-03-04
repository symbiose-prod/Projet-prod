# common/email.py — Brevo (transactionnel) + wrappers retro-compatibles
from __future__ import annotations

import base64
import http.client
import json
import logging
import os

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

_log = logging.getLogger("ferment.email")


class EmailSendError(RuntimeError):
    pass


# Lecture lazy a chaque appel (le .env peut etre charge apres l'import du module)
def _get_api_key() -> str:
    return os.getenv("BREVO_API_KEY", "")


def _get_sender_email() -> str:
    return os.getenv("EMAIL_SENDER") or os.getenv("SENDER_EMAIL") or "hello@symbiose-kefir.fr"


def _get_sender_name() -> str:
    return os.getenv("EMAIL_SENDER_NAME") or os.getenv("SENDER_NAME") or "Symbiose Kefir"


def _require_env() -> tuple[str, str, str]:
    """Valide et retourne (api_key, sender_email, sender_name)."""
    api_key = _get_api_key()
    sender_email = _get_sender_email()
    missing = []
    if not api_key:
        missing.append("BREVO_API_KEY")
    if not sender_email:
        missing.append("SENDER_EMAIL")
    if missing:
        raise EmailSendError(f"Variables d'environnement manquantes: {', '.join(missing)}")
    return api_key, sender_email, _get_sender_name()

def _is_brevo_retryable(exc: BaseException) -> bool:
    """Return True for transient Brevo API errors worth retrying."""
    if isinstance(exc, (ConnectionError, OSError, http.client.HTTPException)):
        return True
    if isinstance(exc, EmailSendError) and "429" in str(exc):
        return True
    return False


_retry_brevo = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception(_is_brevo_retryable),
    before_sleep=before_sleep_log(_log, logging.WARNING),
    reraise=True,
)


@_retry_brevo
def _post_brevo(path: str, payload: dict) -> dict:
    """POST JSON vers l'API Brevo et renvoie le JSON de reponse."""
    api_key, _, _ = _require_env()
    body = json.dumps(payload)
    headers = {
        "api-key": api_key,
        "accept": "application/json",
        "content-type": "application/json",
    }
    try:
        with http.client.HTTPSConnection("api.brevo.com", timeout=20) as conn:  # type: ignore[attr-defined]
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
    except EmailSendError:
        raise
    except (ConnectionError, OSError, http.client.HTTPException) as e:
        _log.error("Echec connexion Brevo: %s", e)
        raise EmailSendError(f"Echec connexion Brevo: {e}") from e

    if resp.status == 429:
        _log.warning("Brevo rate-limit atteint (HTTP 429)")
        raise EmailSendError("Brevo rate-limit atteint. Réessayez dans quelques instants.")
    if resp.status not in (200, 201, 202):
        _log.error("Brevo HTTP %d — reponse: %s", resp.status, raw[:200])
        raise EmailSendError(f"Brevo HTTP {resp.status} — reponse: {raw}")

    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError, KeyError):
        data = {"raw": raw}
    return data

# ---------------------------------------------------------------------------
# 1) Reset password (inchangé)
# ---------------------------------------------------------------------------
def send_reset_email(to_email: str, reset_url: str) -> dict:
    """
    Envoie l'email de réinitialisation via Brevo.
    Retourne {status, provider_msg_id?, response?}.
    Lève EmailSendError en cas d'erreur.
    """
    # version texte + html
    text = (
        "Bonjour,\n\n"
        "Vous avez demandé à réinitialiser votre mot de passe.\n"
        f"Lien de réinitialisation (valable 60 min) : {reset_url}\n\n"
        "Si vous n’êtes pas à l’origine de cette demande, ignorez ce message."
    )
    html = (
        "<p>Bonjour,</p>"
        "<p>Vous avez demandé à réinitialiser votre mot de passe.</p>"
        f'<p><a href="{reset_url}">Réinitialiser mon mot de passe</a></p>'
        "<p>Ce lien expire dans 60 minutes. Si vous n’êtes pas à l’origine de cette demande, ignorez ce message.</p>"
    )

    _, sender_email, sender_name = _require_env()
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email}],
        "subject": "Reinitialisation de votre mot de passe",
        "htmlContent": html,
        "textContent": text,
    }
    data = _post_brevo("/v3/smtp/email", payload)
    _log.info("Email reset envoyé à %s (msg_id=%s)", to_email, data.get("messageId"))
    return {"status": "sent", "provider_msg_id": data.get("messageId"), "response": data}

# ---------------------------------------------------------------------------
# 2) Rétro-compatibilité pour 04_Fiche_de_ramasse.py
#    - send_html_with_pdf(...)
#    - html_signature()
#    - _get_ns(), _get(...)
# ---------------------------------------------------------------------------
def html_signature() -> str:
    """Petit bloc signature HTML par defaut."""
    sender_email = _get_sender_email()
    sender_name = _get_sender_name()
    return (
        "<br><br>"
        "<div style='font-size:12px;color:#666'>"
        f"<strong>{sender_name}</strong><br>"
        f"{sender_email}"
        "</div>"
    )

def _encode_attachments(attachments: list[tuple[str, bytes]] | None) -> list[dict[str, str]]:
    """
    Convertit [(filename, bytes), ...] en format Brevo:
    {"name": "file.pdf", "content": "<base64>"}
    """
    out: list[dict[str, str]] = []
    if not attachments:
        return out
    for name, content in attachments:
        if not content:
            continue
        b64 = base64.b64encode(content).decode("ascii")
        out.append({"name": name, "content": b64})
    return out

def send_html_with_pdf(
    to_email: str,
    subject: str,
    html_body: str,
    attachments: list[tuple[str, bytes]] | None = None,
    reply_to: str | None = None,
) -> dict:
    """
    Envoi générique HTML + pièces jointes (PDF ou autres).
    - attachments: liste [(filename, bytes), ...]
    """
    _, sender_email, sender_name = _require_env()
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_body,
    }
    # ajoute version texte simplifiée
    payload["textContent"] = _strip_html_to_text(html_body)

    # attachments
    att = _encode_attachments(attachments)
    if att:
        payload["attachment"] = att

    # reply-to optionnel
    if reply_to:
        payload["replyTo"] = {"email": reply_to}

    data = _post_brevo("/v3/smtp/email", payload)
    _log.info("Email envoyé à %s — sujet=%s (msg_id=%s)", to_email, subject, data.get("messageId"))
    return {"status": "sent", "provider_msg_id": data.get("messageId"), "response": data}

def _strip_html_to_text(html: str) -> str:
    """Fallback très simple pour avoir un text/plain (pas de dépendance à bs4)."""
    if not html:
        return ""
    # ultra basique: retire les balises
    import re
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    # normalise espaces
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
