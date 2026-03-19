"""
common/_session.py
==================
Shared helpers to read NiceGUI session context (tenant, user).

All modules that need the current tenant_id or user_id should call these
functions instead of duplicating the pattern locally.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("ferment.session")


def current_tenant_id() -> str:
    """Return the tenant_id from the NiceGUI session, or fallback to default.

    Uses a late import of ``nicegui.app`` to avoid circular imports at
    module level.  If no session is active (CLI, background tasks, tests),
    falls back to the default tenant.
    """
    try:
        from nicegui import app
        tid = app.storage.user.get("tenant_id")
        if tid:
            return str(tid)
    except Exception:
        _log.debug("Cannot read tenant from session — using default", exc_info=True)

    from common.storage import DEFAULT_TENANT_NAME, _ensure_tenant
    return _ensure_tenant(DEFAULT_TENANT_NAME)


def current_user_id() -> str | None:
    """Return the current user id from the NiceGUI session, or ``None``."""
    try:
        from nicegui import app
        uid = app.storage.user.get("id")
        return str(uid) if uid else None
    except Exception:
        _log.debug("Cannot read user_id from session", exc_info=True)
        return None
