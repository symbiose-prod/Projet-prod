"""
pages/admin.py
==============
Admin dashboard — accessible uniquement aux utilisateurs avec role=admin.

Pour l'instant : viewer audit log (qui a fait quoi, quand) avec filtres
par action et fenêtre temporelle. Base pour d'autres outils admin futurs
(gestion utilisateurs, rotation clés API, etc.).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from nicegui import ui

from db.conn import run_sql
from pages.auth import require_auth
from pages.theme import page_layout, section_title

_log = logging.getLogger("ferment.admin")


def _require_admin() -> dict | None:
    """Retourne le user si admin, sinon redirige /accueil et retourne None."""
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


def _list_audit(
    tenant_id: str,
    *,
    limit: int = 200,
    action_filter: str | None = None,
    days: int = 30,
) -> list[dict]:
    """Charge les événements audit récents pour le tenant."""
    since = datetime.now(UTC) - timedelta(days=days)
    where_action = " AND action = :a" if action_filter else ""
    params: dict = {"tid": tenant_id, "since": since, "lim": limit}
    if action_filter:
        params["a"] = action_filter
    return run_sql(
        f"""
        SELECT id, tenant_id, user_email, action, details, created_at
        FROM audit_log
        WHERE tenant_id = :tid AND created_at >= :since{where_action}
        ORDER BY created_at DESC
        LIMIT :lim
        """,
        params,
    ) or []


def _list_distinct_actions(tenant_id: str, days: int = 30) -> list[str]:
    """Liste les actions distinctes présentes dans la fenêtre (pour le filtre)."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = run_sql(
        """
        SELECT DISTINCT action
        FROM audit_log
        WHERE tenant_id = :tid AND created_at >= :since
        ORDER BY action
        """,
        {"tid": tenant_id, "since": since},
    ) or []
    return [r["action"] for r in rows]


@ui.page("/admin")
def page_admin():
    user = _require_admin()
    if not user:
        return

    tenant_id = str(user.get("tenant_id", ""))

    with page_layout("Administration", "admin_panel_settings", "/admin"):
        section_title("Journal d'audit", "history")

        # ── Filtres ──
        filters_state = {"days": 30, "action": None}
        filters_row = ui.row().classes("w-full items-center gap-3 q-mb-md")

        table_ref = {"t": None}

        def _refresh():
            try:
                rows = _list_audit(
                    tenant_id,
                    action_filter=filters_state["action"],
                    days=filters_state["days"],
                )
            except Exception:
                _log.warning("Erreur chargement audit log", exc_info=True)
                ui.notify("Erreur chargement journal audit.", type="negative")
                return

            display_rows = []
            for r in rows:
                ts = r.get("created_at")
                ts_str = ts.strftime("%d/%m %H:%M:%S") if hasattr(ts, "strftime") else str(ts)
                details = r.get("details") or {}
                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except ValueError:
                        details = {"_raw": details}
                display_rows.append({
                    "ts": ts_str,
                    "user": r.get("user_email") or "—",
                    "action": r.get("action") or "?",
                    "details": json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                })

            if table_ref["t"] is None:
                table_ref["t"] = ui.table(
                    columns=[
                        {"name": "ts", "label": "Date / Heure", "field": "ts", "align": "left"},
                        {"name": "user", "label": "Utilisateur", "field": "user", "align": "left"},
                        {"name": "action", "label": "Action", "field": "action", "align": "left"},
                        {"name": "details", "label": "Détails", "field": "details", "align": "left"},
                    ],
                    rows=display_rows,
                    row_key="ts",
                    pagination={"rowsPerPage": 25},
                ).classes("w-full").props("flat bordered dense")
            else:
                table_ref["t"].rows = display_rows
                table_ref["t"].update()

            if not display_rows:
                ui.notify("Aucun événement sur la période.", type="info")

        with filters_row:
            ui.label("Période :").classes("text-caption text-grey-7")
            period_select = ui.select(
                {1: "24h", 7: "7 jours", 30: "30 jours", 90: "90 jours"},
                value=30,
                on_change=lambda e: (
                    filters_state.update({"days": int(e.value)}), _refresh()
                ),
            ).props("outlined dense").style("min-width: 120px")

            # Liste actions (une fois, au chargement initial)
            try:
                actions = _list_distinct_actions(tenant_id, days=90)
            except Exception:
                actions = []

            action_options = {None: "Toutes actions", **{a: a for a in actions}}
            ui.select(
                action_options,
                value=None,
                on_change=lambda e: (
                    filters_state.update({"action": e.value}), _refresh()
                ),
            ).props("outlined dense").style("min-width: 240px")

            ui.button(
                "Rafraîchir",
                icon="refresh",
                on_click=_refresh,
            ).props("flat dense color=grey-7")

            # Kept for type-checker pleasure
            _ = period_select

        _refresh()
