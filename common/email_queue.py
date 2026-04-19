"""
common/email_queue.py
=====================
File d'attente DB-backed pour les emails.

Modèle : si l'envoi via Brevo échoue (rate-limit, réseau, panne du provider),
l'email est persisté en status='pending' avec son payload complet. Un worker
(ou cron) appelle :func:`retry_pending_emails` pour rééssayer les envois.

Design :
- Persistance simple (table ``email_queue``, 1 ligne = 1 email).
- Pas de worker thread intégré — l'ordonnancement est externe (cron /
  systemd timer / tâche manuelle depuis l'admin UI).
- Backoff exponentiel côté retry : après 5 échecs, on passe en ``failed``
  pour éviter de matraquer Brevo pour un email voué à échouer (email
  malformé, destinataire invalide, etc.). Un humain intervient ensuite.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from db.conn import run_sql

_log = logging.getLogger("ferment.email_queue")

# Après N tentatives échouées, on renonce et on marque ``failed``.
MAX_ATTEMPTS = 5

# Max items traités par passage de retry_pending_emails (protection contre
# les rafales trop longues qui bloqueraient le cron).
DEFAULT_BATCH_SIZE = 20


def _encode_attachments(attachments: list[tuple[str, bytes]] | None) -> list[dict]:
    """Sérialise les pièces jointes en base64 pour stockage JSON."""
    if not attachments:
        return []
    out = []
    for name, content in attachments:
        if content is None:
            continue
        out.append({
            "name": name,
            "content_b64": base64.b64encode(content).decode("ascii"),
        })
    return out


def _decode_attachments(attachments_json: Any) -> list[tuple[str, bytes]]:
    """Désérialise les pièces jointes depuis la DB."""
    if isinstance(attachments_json, str):
        try:
            attachments_json = json.loads(attachments_json)
        except ValueError:
            return []
    if not isinstance(attachments_json, list):
        return []
    out: list[tuple[str, bytes]] = []
    for item in attachments_json:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        b64 = item.get("content_b64", "")
        try:
            content = base64.b64decode(b64)
        except (ValueError, TypeError):
            continue
        if name and content:
            out.append((name, content))
    return out


def enqueue(
    *,
    to_emails: list[str],
    subject: str,
    html_body: str,
    cc_emails: list[str] | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
    reply_to: str | None = None,
    tenant_id: str | None = None,
    last_error: str | None = None,
) -> int | None:
    """Persiste un email en status='pending'. Retourne l'id ou None si échec DB.

    Cette fonction NE fait PAS de tentative d'envoi immédiate — elle se borne
    à queueue. L'appelant décide (tenter puis fallback, ou queue direct).
    """
    try:
        rows = run_sql(
            """
            INSERT INTO email_queue (
                tenant_id, to_emails, cc_emails, subject, html_body,
                attachments, reply_to, status, last_error
            ) VALUES (
                :tid, :to, :cc, :subj, :body,
                CAST(:att AS jsonb), :rt, 'pending', :err
            )
            RETURNING id
            """,
            {
                "tid": tenant_id,
                "to": to_emails,
                "cc": cc_emails or [],
                "subj": subject,
                "body": html_body,
                "att": json.dumps(_encode_attachments(attachments)),
                "rt": reply_to,
                "err": last_error,
            },
        )
        qid = int(rows[0]["id"]) if rows else None
        _log.info("Email queued (id=%s, subject=%r)", qid, subject[:50])
        return qid
    except (SQLAlchemyError, OSError):
        _log.exception("Échec enqueue email (subject=%r)", subject[:50])
        return None


def _mark_sent(queue_id: int, provider_msg_id: str | None) -> None:
    run_sql(
        """
        UPDATE email_queue
        SET status = 'sent',
            provider_msg_id = :pid,
            sent_at = now(),
            updated_at = now()
        WHERE id = :id
        """,
        {"id": queue_id, "pid": provider_msg_id},
    )


def _mark_retry(queue_id: int, error: str, attempts: int) -> None:
    # Si trop de tentatives, on abandonne
    new_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    run_sql(
        """
        UPDATE email_queue
        SET status = :st,
            attempts = :n,
            last_error = :err,
            updated_at = now()
        WHERE id = :id
        """,
        {"id": queue_id, "st": new_status, "n": attempts, "err": error[:500]},
    )


def list_pending(limit: int = DEFAULT_BATCH_SIZE) -> list[dict[str, Any]]:
    """Liste les emails en attente de retry, les plus anciens en premier."""
    return run_sql(
        """
        SELECT id, tenant_id, to_emails, cc_emails, subject, html_body,
               attachments, reply_to, attempts
        FROM email_queue
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT :lim
        """,
        {"lim": limit},
    ) or []


def retry_pending_emails(batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, int]:
    """Tente de renvoyer les emails en status='pending'.

    À appeler périodiquement (cron). Retourne un résumé
    ``{"attempted": N, "sent": N, "retried": N, "failed": N}``.

    Les envois utilisent :func:`common.email.send_html_with_pdf` — donc
    héritent de son retry tenacity interne. En cas d'échec persistant,
    on incrémente ``attempts`` et on repasse le mail en pending (sauf si
    MAX_ATTEMPTS atteint, auquel cas status = 'failed').
    """
    from common.email import EmailSendError, send_html_with_pdf

    summary = {"attempted": 0, "sent": 0, "retried": 0, "failed": 0}

    pending = list_pending(batch_size)
    for row in pending:
        summary["attempted"] += 1
        qid = int(row["id"])
        attempts = int(row.get("attempts") or 0) + 1
        to_emails = row.get("to_emails") or []
        cc_emails = row.get("cc_emails") or None
        subject = row.get("subject") or "(sans sujet)"
        html_body = row.get("html_body") or ""
        attachments = _decode_attachments(row.get("attachments"))
        reply_to = row.get("reply_to")

        try:
            resp = send_html_with_pdf(
                to_email=to_emails if len(to_emails) > 1 else (to_emails[0] if to_emails else ""),
                subject=subject,
                html_body=html_body,
                attachments=attachments or None,
                reply_to=reply_to,
                cc=cc_emails if cc_emails else None,
            )
            _mark_sent(qid, resp.get("provider_msg_id"))
            summary["sent"] += 1
            _log.info("Email queue id=%s envoyé (attempt=%d)", qid, attempts)
        except (EmailSendError, OSError, RuntimeError) as exc:
            _mark_retry(qid, str(exc), attempts)
            if attempts >= MAX_ATTEMPTS:
                summary["failed"] += 1
                _log.error(
                    "Email queue id=%s FAILED après %d tentatives : %s",
                    qid, attempts, exc,
                )
            else:
                summary["retried"] += 1
                _log.warning(
                    "Email queue id=%s retry (attempt=%d/%d): %s",
                    qid, attempts, MAX_ATTEMPTS, exc,
                )
    return summary


def send_with_queue_fallback(
    to_email: str | list[str],
    subject: str,
    html_body: str,
    attachments: list[tuple[str, bytes]] | None = None,
    reply_to: str | None = None,
    cc: list[str] | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Wrapper autour de send_html_with_pdf avec fallback vers la queue DB.

    Comportement :
    1. Tente l'envoi immédiat via Brevo (tenacity retry interne).
    2. Si échec (panne Brevo, 429, réseau), queue l'email en DB et retourne
       ``{"status": "queued", "queue_id": N}`` au lieu de lever.
    3. Si échec ET échec du queue (DB down), relève l'erreur originale.

    Utile pour les emails utilisateur-critiques (confirmation, reset pwd)
    où on préfère garantir l'envoi différé plutôt que de perdre l'email.
    """
    from common.email import EmailSendError, send_html_with_pdf

    to_list = [to_email] if isinstance(to_email, str) else list(to_email)

    try:
        return send_html_with_pdf(
            to_email=to_email,
            subject=subject,
            html_body=html_body,
            attachments=attachments,
            reply_to=reply_to,
            cc=cc,
        )
    except (EmailSendError, OSError, RuntimeError) as exc:
        _log.warning(
            "Envoi Brevo échoué (subject=%r, to=%s) — fallback queue DB : %s",
            subject[:50], to_list, exc,
        )
        qid = enqueue(
            to_emails=to_list,
            cc_emails=cc,
            subject=subject,
            html_body=html_body,
            attachments=attachments,
            reply_to=reply_to,
            tenant_id=tenant_id,
            last_error=str(exc)[:500],
        )
        if qid is None:
            # Queue elle-même a échoué → relève l'erreur originale
            raise
        return {"status": "queued", "queue_id": qid, "error": str(exc)}
