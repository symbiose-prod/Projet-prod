"""
pages/admin_eb_outbox.py
========================
Dashboard admin /admin/eb-outbox : monitoring de la queue Outbox EasyBeer.

Permet de voir l'état des events poussés vers EB :
- Pending : events en attente de retry
- Sent : events traités avec succès
- Dead : events qui ont atteint max_attempts sans succès (à investiguer)

Actions disponibles :
- Retry manuel d'un event dead
- Voir le payload complet (modal)
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from nicegui import ui

from common.outbox import get_stats, retry_event
from db.conn import run_sql
from pages.admin import _require_admin
from pages.theme import page_layout, section_title

_log = logging.getLogger("ferment.admin_eb_outbox")


def _list_events(
    tenant_id: str,
    *,
    status: str,
    limit: int = 100,
    days: int = 7,
) -> list[dict]:
    """Charge les events d'un status donné pour le tenant courant."""
    since = datetime.now(UTC) - timedelta(days=days)
    return run_sql(
        """
        SELECT id, tenant_id, event_type, payload, status,
               attempt_count, max_attempts, last_error,
               next_retry_at, created_at, sent_at, created_by
        FROM eb_outbox
        WHERE tenant_id = :tid
          AND status = :st
          AND created_at >= :since
        ORDER BY created_at DESC
        LIMIT :lim
        """,
        {"tid": tenant_id, "st": status, "since": since, "lim": limit},
    ) or []


def _format_payload(payload: dict | str) -> str:
    """Formate le payload pour affichage modal (JSON pretty-printed)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return str(payload)
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _show_payload_dialog(event: dict) -> None:
    """Modal affichant le payload JSON complet + métadonnées."""
    with ui.dialog() as dialog, ui.card().classes("w-[800px] max-w-full"):
        ui.label(f"Event #{event['id']} — {event['event_type']}").classes("text-lg font-bold")
        ui.label(f"Status : {event['status']}").classes("text-sm text-gray-600")
        ui.label(f"Créé par : {event.get('created_by') or '(inconnu)'}").classes("text-sm text-gray-600")
        ui.label(f"Tentatives : {event['attempt_count']}/{event['max_attempts']}").classes(
            "text-sm text-gray-600"
        )
        if event.get("last_error"):
            ui.label("Dernière erreur :").classes("text-sm font-semibold mt-2")
            ui.label(str(event["last_error"])).classes(
                "text-xs text-red-700 bg-red-50 p-2 rounded font-mono whitespace-pre-wrap"
            )
        ui.label("Payload :").classes("text-sm font-semibold mt-2")
        ui.code(_format_payload(event.get("payload") or {})).classes(
            "max-h-[400px] overflow-auto w-full"
        )
        with ui.row().classes("justify-end gap-2 mt-2"):
            ui.button("Fermer", on_click=dialog.close)
    dialog.open()


def _retry_event_with_feedback(event_id: int, refresh_fn) -> None:
    """Tente le retry et notifie l'utilisateur."""
    ok = retry_event(event_id)
    if ok:
        ui.notify(f"Event #{event_id} remis en pending — sera retenté au prochain tick.", type="positive")
        refresh_fn()
    else:
        ui.notify(
            f"Échec retry event #{event_id} (n'existe pas ou pas en status 'dead').",
            type="negative",
        )


def _render_events_table(events: list[dict], *, with_retry: bool, refresh_fn) -> None:
    """Affiche un tableau d'events avec actions."""
    if not events:
        ui.label("Aucun event dans cette catégorie.").classes("text-gray-500 italic")
        return

    columns = [
        {"name": "id", "label": "#", "field": "id", "align": "left", "sortable": True},
        {"name": "event_type", "label": "Type", "field": "event_type", "align": "left"},
        {"name": "attempts", "label": "Tentatives", "field": "attempts", "align": "center"},
        {"name": "created_at", "label": "Créé", "field": "created_at", "align": "left"},
        {"name": "created_by", "label": "Auteur", "field": "created_by", "align": "left"},
        {"name": "actions", "label": "Actions", "field": "actions", "align": "right"},
    ]
    if with_retry:
        columns.insert(-1, {"name": "last_error", "label": "Erreur", "field": "last_error", "align": "left"})

    rows = []
    for e in events:
        rows.append(
            {
                "id": e["id"],
                "event_type": e["event_type"],
                "attempts": f"{e['attempt_count']}/{e['max_attempts']}",
                "created_at": e["created_at"].strftime("%Y-%m-%d %H:%M") if e.get("created_at") else "",
                "created_by": e.get("created_by") or "—",
                "last_error": (e.get("last_error") or "")[:80],
                "_raw": e,
            }
        )

    table = ui.table(columns=columns, rows=rows, row_key="id").classes("w-full")
    table.add_slot(
        "body-cell-actions",
        r"""
        <q-td :props="props" class="text-right">
          <q-btn dense flat icon="visibility" @click="$parent.$emit('view', props.row)" />
          {{retry_btn}}
        </q-td>
        """.replace(
            "{{retry_btn}}",
            '<q-btn dense flat color="primary" icon="replay" @click="$parent.$emit(\'retry\', props.row)" />'
            if with_retry
            else "",
        ),
    )
    table.on("view", lambda e: _show_payload_dialog(e.args["_raw"]))
    if with_retry:
        table.on("retry", lambda e: _retry_event_with_feedback(int(e.args["id"]), refresh_fn))


@ui.page("/admin/eb-outbox")
def page_admin_eb_outbox() -> None:
    """Dashboard outbox EB — accessible uniquement aux admins."""
    user = _require_admin()
    if not user:
        return

    tenant_id = user["tenant_id"]
    refresh_holder: dict[str, object] = {"fn": lambda: None}

    with page_layout("Outbox EasyBeer", "sync_alt", "/admin/eb-outbox"):
        section_title("Synchronisation des écritures vers Easybeer", "cloud_sync")

        # — Stats overview
        stats_holder = ui.column().classes("w-full")

        def render_stats() -> None:
            stats_holder.clear()
            stats = get_stats(tenant_id)
            with stats_holder:
                with ui.row().classes("gap-4 mb-4"):
                    for label, key, color in [
                        ("Pending", "pending", "blue"),
                        ("Sent", "sent", "green"),
                        ("Dead", "dead", "red"),
                    ]:
                        with ui.card().classes(f"p-3 min-w-[120px] border-l-4 border-{color}-500"):
                            ui.label(label).classes("text-xs text-gray-500 uppercase")
                            ui.label(str(stats.get(key, 0))).classes(f"text-2xl font-bold text-{color}-700")

        # — Refresh button
        with ui.row().classes("gap-2 mb-2"):
            ui.button(
                "Rafraîchir",
                icon="refresh",
                on_click=lambda: refresh_holder["fn"](),  # type: ignore[operator]
            ).props("outline")

        render_stats()

        # — Tabs par status
        with ui.tabs().classes("w-full") as tabs:
            tab_pending = ui.tab("Pending", icon="schedule")
            tab_sent = ui.tab("Sent", icon="check_circle")
            tab_dead = ui.tab("Dead", icon="error")

        # Panels containers
        panel_holders: dict[str, ui.column] = {}

        with ui.tab_panels(tabs, value=tab_pending).classes("w-full"):
            with ui.tab_panel(tab_pending):
                panel_holders["pending"] = ui.column().classes("w-full")
            with ui.tab_panel(tab_sent):
                panel_holders["sent"] = ui.column().classes("w-full")
            with ui.tab_panel(tab_dead):
                panel_holders["dead"] = ui.column().classes("w-full")

        def refresh_all() -> None:
            render_stats()
            for status_name in ("pending", "sent", "dead"):
                panel = panel_holders[status_name]
                panel.clear()
                with panel:
                    events = _list_events(tenant_id, status=status_name, limit=100, days=30)
                    _render_events_table(
                        events,
                        with_retry=(status_name == "dead"),
                        refresh_fn=refresh_all,
                    )

        refresh_holder["fn"] = refresh_all
        refresh_all()
