"""
pages/tags.py
=============
Page Tags clients — Affiche les tags EasyBeer avec nombre de clients.
Permet de relancer la synchronisation manuellement.
"""
from __future__ import annotations

import asyncio
import logging

from nicegui import ui

from pages.auth import require_auth
from pages.theme import page_layout, section_title

_log = logging.getLogger("ferment.tags")


@ui.page("/tags")
async def page_tags():
    user = require_auth()
    if not user:
        return

    with page_layout("Tags clients", "sell", "/tags"):

        from common._session import current_tenant_id
        from common.client_cache import get_all_tags, get_all_tournees, get_all_types, get_last_sync

        tid = current_tenant_id()
        last_sync = get_last_sync(tid)

        # ── Info sync ──
        with ui.row().classes("w-full items-center gap-3"):
            if last_sync:
                ui.label(f"Dernière synchronisation : {last_sync[:16].replace('T', ' ')}").classes(
                    "text-caption text-grey-6"
                )
            else:
                ui.label("Aucune synchronisation effectuée.").classes(
                    "text-caption text-orange-8"
                )

            async def do_sync():
                sync_btn.disable()
                sync_status.text = "Synchronisation en cours..."
                sync_status.classes("text-blue-8")
                try:
                    from common.client_cache import sync_clients
                    from common.easybeer import is_configured as eb_ok

                    if not eb_ok():
                        sync_status.text = "EasyBeer non configuré."
                        sync_status.classes("text-negative")
                        return

                    result = await asyncio.to_thread(sync_clients, tid)
                    sync_status.text = (
                        f"Sync terminée : {result['clients']} clients, {result['tags']} tags"
                    )
                    sync_status.classes("text-positive")
                    # Recharger la page pour afficher les nouvelles données
                    await asyncio.sleep(1)
                    ui.navigate.to("/tags")
                except Exception as exc:
                    _log.exception("Erreur sync clients")
                    sync_status.text = f"Erreur : {exc}"
                    sync_status.classes("text-negative")
                finally:
                    sync_btn.enable()

            sync_btn = ui.button(
                "Synchroniser", icon="sync", on_click=do_sync,
            ).props("outline color=green-8 size=sm")
            sync_status = ui.label("").classes("text-caption")

        # ── Tags ──
        tags = get_all_tags(tid)
        section_title(f"Tags ({len(tags)})", "sell")

        if not tags:
            ui.label(
                "Aucun tag trouvé. Lance une synchronisation pour charger les données."
            ).classes("text-grey-6 q-pa-md")
        else:
            tag_columns = [
                {"name": "tag", "label": "Tag", "field": "tag", "align": "left", "sortable": True},
                {"name": "count", "label": "Clients", "field": "count", "align": "right", "sortable": True},
            ]
            tag_rows = [{"tag": t["tag"], "count": t["client_count"]} for t in tags]

            ui.table(
                columns=tag_columns,
                rows=tag_rows,
                row_key="tag",
                pagination={"rowsPerPage": 0},
            ).classes("w-full").props("flat bordered dense")

        # ── Tournées ──
        tournees = get_all_tournees(tid)
        section_title(f"Tournées ({len(tournees)})", "local_shipping")

        if tournees:
            with ui.row().classes("w-full gap-2 flex-wrap"):
                for t in tournees:
                    ui.badge(t, color="blue-2", text_color="blue-9").props("outline")
        else:
            ui.label("Aucune tournée.").classes("text-grey-6")

        # ── Types clients ──
        types = get_all_types(tid)
        section_title(f"Types clients ({len(types)})", "category")

        if types:
            type_columns = [
                {"name": "type", "label": "Type", "field": "type_libelle", "align": "left", "sortable": True},
                {"name": "parent", "label": "Type parent", "field": "type_parent", "align": "left", "sortable": True},
            ]
            type_rows = [{"type_libelle": t["type_libelle"], "type_parent": t["type_parent"] or "—"} for t in types]

            ui.table(
                columns=type_columns,
                rows=type_rows,
                row_key="type_libelle",
                pagination={"rowsPerPage": 0},
            ).classes("w-full").props("flat bordered dense")
        else:
            ui.label("Aucun type client.").classes("text-grey-6")
