"""
common/audit.py
===============
Journal d'audit — traçabilité des actions métier.

Chaque événement est persisté dans la table audit_log (INSERT fire-and-forget).
Les échecs d'écriture sont loggés mais ne bloquent jamais l'appelant.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from db.conn import run_sql

_log = logging.getLogger("ferment.audit")

# Actions reconnues (non exhaustif — on accepte tout str <= 50 chars)
ACTION_LOGIN = "login"
ACTION_LOGIN_FAILED = "login_failed"
ACTION_LOGOUT = "logout"
ACTION_PRODUCTION_SAVED = "production_saved"
ACTION_BRASSIN_CREATED = "brassin_created"
ACTION_FILE_UPLOADED = "file_uploaded"


def log_event(
    *,
    tenant_id: str | None = None,
    user_email: str | None = None,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Insère un événement dans audit_log. Ne lève jamais d'exception."""
    try:
        run_sql(
            """
            INSERT INTO audit_log (tenant_id, user_email, action, details)
            VALUES (:t, :e, :a, CAST(:d AS jsonb))
            """,
            {
                "t": tenant_id,
                "e": user_email,
                "a": action[:50],
                "d": _json_dumps(details or {}),
            },
        )
    except (SQLAlchemyError, OSError):
        _log.warning("Échec écriture audit_log: %s %s %s", action, user_email, details, exc_info=True)


def _json_dumps(obj: Any) -> str:
    """Sérialise en JSON (import local pour éviter le coût au module-level)."""
    import json
    return json.dumps(obj, default=str, ensure_ascii=False)
