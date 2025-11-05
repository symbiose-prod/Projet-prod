# common/email.py — Brevo (transactionnel) + wrappers rétro-compatibles
from __future__ import annotations
import os, json, http.client, base64
from typing import Optional, List, Tuple, Dict, Any

BREVO_API_KEY = os.getenv("BREVO_API_KEY")
SENDER_EMAIL  = os.getenv("SENDER_EMAIL", "station.ferment@gmail.com")
SENDER_NAME   = os.getenv("SENDER_NAME", "Ferment Station")

class EmailSendError(RuntimeError):
    pass

def _require_env():
    missing = []
    if not BREVO_API_KEY:
        missing.append("BREVO_API_KEY")
    if not SENDER_EMAIL:
        missing.append("SENDER_EMAIL")
    if missing:
        raise EmailSendError(f"Variables d'environnement manquantes: {', '.join(missing)}")

def _post_brevo(path: str, payload: dict) -> dict:
    """POST JSON vers l'API Brevo et renvoie le JSON de réponse."""
    _require_env()
    body = json.dumps(payload)
    try:
        conn = http.client.HTTPSConnection("api.brevo.com", timeout=20)
        try:
            headers = {
                "api-key": BREVO_API_KEY,
                "accept": "application/json",
                "content-type": "application/json",
            }
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
        finally:
            conn.close()
    except Exception as e:
        raise EmailSendError(f"Echec connexion Brevo: {e}") from e

    if resp.status not in (200, 201, 202):
        raise EmailSendError(f"Brevo HTTP {resp.status} — réponse: {raw}")

    try:
        data = json.loads(raw) if raw else {}
    except Exception:
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

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": "Réinitialisation de votre mot de passe",
        "htmlContent": html,
        "textContent": text,
    }
    data = _post_brevo("/v3/smtp/email", payload)
    return {"status": "sent", "provider_msg_id": data.get("messageId"), "response": data}

# ---------------------------------------------------------------------------
# 2) Rétro-compatibilité pour 04_Fiche_de_ramasse.py
#    - send_html_with_pdf(...)
#    - html_signature()
#    - _get_ns(), _get(...)
# ---------------------------------------------------------------------------
def html_signature() -> str:
    """Petit bloc signature HTML par défaut (tu peux le personnaliser)."""
    return (
        "<br><br>"
        "<div style='font-size:12px;color:#666'>"
        f"<strong>{SENDER_NAME}</strong><br>"
        f"{SENDER_EMAIL}"
        "</div>"
    )

def _get_ns() -> dict:
    """Ancien helper de templating : on renvoie un dict vide pour compat."""
    return {}

def _get(key: str, default: Any = None) -> Any:
    """Ancien helper de templating : renvoie simplement la valeur par défaut."""
    return default

def _encode_attachments(attachments: Optional[List[Tuple[str, bytes]]]) -> List[Dict[str, str]]:
    """
    Convertit [(filename, bytes), ...] en format Brevo:
    {"name": "file.pdf", "content": "<base64>"}
    """
    out: List[Dict[str, str]] = []
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
    attachments: Optional[List[Tuple[str, bytes]]] = None,
    reply_to: Optional[str] = None,
) -> dict:
    """
    Envoi générique HTML + pièces jointes (PDF ou autres).
    - attachments: liste [(filename, bytes), ...]
    """
    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
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
