"""
ui/sync.py
==========
Page Synchronisation étiquettes — monitoring, déclenchement manuel, gestion clés API.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from nicegui import app, ui

from ui.auth import require_auth
from ui.theme import COLORS, page_layout, section_title, kpi_card

_log = logging.getLogger("ferment.sync.ui")


# ─── Helpers DB ──────────────────────────────────────────────────────────────


def _get_recent_operations(tenant_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Récupère les N dernières opérations de sync pour un tenant."""
    from db.conn import run_sql
    rows = run_sql(
        """SELECT id, op_type, status, product_count, triggered_by,
                  error_msg, created_at, fetched_at, applied_at
           FROM sync_operations
           WHERE tenant_id = :t
           ORDER BY created_at DESC
           LIMIT :n""",
        {"t": tenant_id, "n": limit},
    )
    return rows if isinstance(rows, list) else []


def _get_last_successful(tenant_id: str) -> dict[str, Any] | None:
    """Dernière opération appliquée avec succès."""
    from db.conn import run_sql
    rows = run_sql(
        """SELECT id, product_count, applied_at
           FROM sync_operations
           WHERE tenant_id = :t AND status = 'applied'
           ORDER BY applied_at DESC
           LIMIT 1""",
        {"t": tenant_id},
    )
    return rows[0] if rows else None


# ─── Formatage ───────────────────────────────────────────────────────────────

_STATUS_LABELS = {
    "pending": ("En attente", "schedule", COLORS["warning"]),
    "fetched": ("Récupérée", "cloud_download", COLORS["blue"]),
    "applied": ("Appliquée", "check_circle", COLORS["success"]),
    "error": ("Erreur", "error", COLORS["error"]),
}


def _fmt_dt(val: Any) -> str:
    """Formate un datetime en string locale lisible."""
    if not val:
        return "—"
    if hasattr(val, "strftime"):
        return val.strftime("%d/%m/%Y %H:%M")
    return str(val)[:16]


def _status_badge(status: str) -> None:
    """Affiche un badge coloré pour le statut."""
    label, icon, color = _STATUS_LABELS.get(status, ("?", "help", COLORS["ink2"]))
    with ui.row().classes("items-center gap-1 no-wrap"):
        ui.icon(icon, size="xs").style(f"color: {color}")
        ui.label(label).classes("text-caption").style(f"color: {color}; font-weight: 500")


# ─── Page ────────────────────────────────────────────────────────────────────


@ui.page("/sync")
def page_sync():
    user = require_auth()
    if not user:
        return

    tenant_id = user.get("tenant_id", "")

    with page_layout("Étiquettes", "label", "/sync") as sidebar:

        with sidebar:
            ui.label("Sync SaaS → Access").classes("text-subtitle2 text-grey-7")
            ui.label(
                "Synchronisation des données de production "
                "vers la base d'impression d'étiquettes."
            ).classes("text-caption text-grey-5")

        # ── KPIs ─────────────────────────────────────────────────────
        last_ok = _get_last_successful(tenant_id)
        ops = _get_recent_operations(tenant_id)

        with ui.row().classes("w-full gap-3"):
            kpi_card(
                "check_circle",
                "Dernière sync OK",
                _fmt_dt(last_ok["applied_at"]) if last_ok else "Jamais",
                COLORS["success"],
            )
            kpi_card(
                "inventory_2",
                "Produits",
                str(last_ok["product_count"]) if last_ok else "0",
                COLORS["green"],
            )
            kpi_card(
                "sync",
                "Opérations récentes",
                str(len(ops)),
                COLORS["blue"],
            )

        # ── Trigger manuel ───────────────────────────────────────────
        with ui.card().classes("w-full").props("flat bordered"):
            with ui.card_section().classes("q-pa-md"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("play_circle", size="sm").style(f"color: {COLORS['green']}")
                    ui.label("Synchronisation manuelle").classes("text-h6")

                ui.label(
                    "Déclenche immédiatement la collecte des données EasyBeer "
                    "(brassins en cours) et crée une opération de sync en attente."
                ).classes("text-body2 q-mt-xs").style(f"color: {COLORS['ink2']}; line-height: 1.6")

                trigger_status = ui.label("").classes("text-body2 q-mt-sm")
                trigger_status.set_visibility(False)

                trigger_spinner = ui.spinner("dots", size="lg", color="green-8").classes("q-mt-sm")
                trigger_spinner.set_visibility(False)

                async def do_trigger():
                    trigger_btn.disable()
                    trigger_spinner.set_visibility(True)
                    trigger_status.set_visibility(False)
                    try:
                        from common.sync.collector import collect_label_data
                        from common.sync import create_sync_operation

                        products = await asyncio.wait_for(
                            asyncio.to_thread(collect_label_data),
                            timeout=120,
                        )

                        if not products:
                            trigger_status.text = "Aucun produit trouvé (pas de brassin en cours ?)."
                            trigger_status.style(f"color: {COLORS['warning']}")
                            trigger_status.set_visibility(True)
                            ui.notify("Sync vide — aucun brassin en cours", type="warning")
                        else:
                            op = create_sync_operation(products, tenant_id=tenant_id, triggered_by="manual")
                            trigger_status.text = (
                                f"Opération #{op['id']} créée — "
                                f"{op['product_count']} produits en attente."
                            )
                            trigger_status.style(f"color: {COLORS['success']}")
                            trigger_status.set_visibility(True)
                            ui.notify(f"Sync créée : {op['product_count']} produits", type="positive")
                            # Rafraîchir le tableau
                            _refresh_table(tenant_id)

                    except TimeoutError:
                        trigger_status.text = "La collecte a dépassé le délai (2 min). Réessayez."
                        trigger_status.style(f"color: {COLORS['error']}")
                        trigger_status.set_visibility(True)
                        ui.notify("Timeout collecte EasyBeer", type="negative")
                    except Exception:
                        _log.exception("Erreur trigger sync manuelle")
                        trigger_status.text = "Erreur lors de la collecte. Consultez les logs."
                        trigger_status.style(f"color: {COLORS['error']}")
                        trigger_status.set_visibility(True)
                        ui.notify("Erreur sync", type="negative")
                    finally:
                        trigger_spinner.set_visibility(False)
                        trigger_btn.enable()

                trigger_btn = ui.button(
                    "Lancer la sync maintenant",
                    icon="sync",
                    on_click=do_trigger,
                ).classes("q-mt-md").props("color=green-8 unelevated")

        # ── Historique des opérations ─────────────────────────────────
        section_title("Historique des syncs", "history")

        ops_container = ui.column().classes("w-full gap-0")

        def _refresh_table(tid: str):
            """Recharge le tableau des opérations."""
            ops_container.clear()
            fresh_ops = _get_recent_operations(tid)
            with ops_container:
                _build_ops_table(fresh_ops)

        def _build_ops_table(operations: list[dict]):
            if not operations:
                ui.label("Aucune opération pour le moment.").classes(
                    "text-body2 text-grey-5 q-pa-md"
                )
                return

            with ui.card().classes("w-full").props("flat bordered"):
                for i, op in enumerate(operations):
                    if i > 0:
                        ui.separator()
                    with ui.card_section().classes("q-pa-md"):
                        with ui.row().classes("w-full items-center gap-3"):
                            # Numéro + statut
                            with ui.column().classes("gap-0").style("min-width: 120px"):
                                ui.label(f"#{op['id']}").classes("text-subtitle2").style(
                                    f"color: {COLORS['ink']}; font-weight: 600"
                                )
                                _status_badge(op["status"])

                            # Détails
                            with ui.column().classes("gap-0 flex-1"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.icon("inventory_2", size="xs").style(f"color: {COLORS['ink2']}")
                                    ui.label(f"{op['product_count']} produits").classes(
                                        "text-body2"
                                    ).style(f"color: {COLORS['ink']}")
                                    ui.label("·").style(f"color: {COLORS['ink2']}")
                                    trig = "Manuel" if op.get("triggered_by") == "manual" else "Planifié"
                                    ui.label(trig).classes("text-caption").style(
                                        f"color: {COLORS['ink2']}"
                                    )

                                # Dates
                                with ui.row().classes("items-center gap-2"):
                                    ui.icon("schedule", size="xs").style(f"color: {COLORS['ink2']}")
                                    ui.label(f"Créée : {_fmt_dt(op['created_at'])}").classes(
                                        "text-caption"
                                    ).style(f"color: {COLORS['ink2']}")
                                    if op.get("applied_at"):
                                        ui.label(f"· Appliquée : {_fmt_dt(op['applied_at'])}").classes(
                                            "text-caption"
                                        ).style(f"color: {COLORS['ink2']}")

                            # Erreur éventuelle
                            if op.get("error_msg"):
                                with ui.element("div").classes("q-pa-xs q-mt-xs").style(
                                    f"background: {COLORS['error']}10; border-radius: 4px; max-width: 400px"
                                ):
                                    ui.label(op["error_msg"][:200]).classes(
                                        "text-caption"
                                    ).style(f"color: {COLORS['error']}")

        with ops_container:
            _build_ops_table(ops)

        # Bouton rafraîchir
        ui.button(
            "Rafraîchir",
            icon="refresh",
            on_click=lambda: _refresh_table(tenant_id),
        ).classes("q-mt-sm").props("flat color=grey-7 dense")

        # ── Clés API ─────────────────────────────────────────────────
        section_title("Clés API (agent Windows)", "vpn_key")

        keys_container = ui.column().classes("w-full gap-2")

        def _refresh_keys():
            """Recharge la liste des clés API."""
            keys_container.clear()
            with keys_container:
                _build_keys_section(tenant_id, user.get("user_id"))

        def _build_keys_section(tid: str, user_id: str | None):
            from common.sync.api_key import list_api_keys

            keys = list_api_keys(tid)

            if not keys:
                ui.label("Aucune clé API. Créez-en une pour connecter l'agent Windows.").classes(
                    "text-body2 text-grey-5 q-pa-sm"
                )
            else:
                with ui.card().classes("w-full").props("flat bordered"):
                    for i, key in enumerate(keys):
                        if i > 0:
                            ui.separator()
                        with ui.card_section().classes("q-pa-md"):
                            with ui.row().classes("w-full items-center justify-between"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.icon(
                                        "vpn_key", size="xs"
                                    ).style(f"color: {COLORS['green'] if key['is_active'] else COLORS['ink2']}")
                                    with ui.column().classes("gap-0"):
                                        label_text = key.get("label") or "Sans nom"
                                        ui.label(label_text).classes("text-body2").style(
                                            f"color: {COLORS['ink']}; font-weight: 500"
                                        )
                                        with ui.row().classes("gap-2"):
                                            ui.label(f"Créée : {_fmt_dt(key['created_at'])}").classes(
                                                "text-caption"
                                            ).style(f"color: {COLORS['ink2']}")
                                            if key.get("last_used"):
                                                ui.label(f"· Utilisée : {_fmt_dt(key['last_used'])}").classes(
                                                    "text-caption"
                                                ).style(f"color: {COLORS['ink2']}")

                                if key["is_active"]:
                                    kid = str(key["id"])

                                    def _do_revoke(key_id=kid):
                                        from common.sync.api_key import revoke_api_key
                                        revoke_api_key(key_id)
                                        ui.notify("Clé révoquée", type="info")
                                        _refresh_keys()

                                    ui.button(
                                        "Révoquer", icon="block",
                                        on_click=_do_revoke,
                                    ).props("flat dense color=red-7")
                                else:
                                    ui.label("Révoquée").classes("text-caption").style(
                                        f"color: {COLORS['ink2']}"
                                    )

            # Bouton générer nouvelle clé
            new_key_label = ui.input("Nom de la clé", placeholder="ex: PC Étiquettes").props(
                "outlined dense"
            ).classes("q-mt-md").style("max-width: 300px")

            def _generate():
                from common.sync.api_key import generate_api_key

                label = (new_key_label.value or "").strip() or "Agent Windows"
                raw = generate_api_key(
                    tenant_id=tid,
                    created_by=user_id,
                    label=label,
                )
                new_key_label.set_value("")
                _refresh_keys()

                # Dialogue modal pour copier la clé
                with ui.dialog().props("persistent") as dlg, \
                        ui.card().style("min-width: 480px; max-width: 600px"):
                    with ui.card_section().classes("q-pa-lg"):
                        with ui.row().classes("items-center gap-2 q-mb-md"):
                            ui.icon("vpn_key", size="sm").style(f"color: {COLORS['green']}")
                            ui.label("Clé API générée").classes("text-h6")

                        ui.label(
                            "Copiez cette clé maintenant. "
                            "Elle ne sera plus visible après fermeture."
                        ).classes("text-body2 q-mb-md").style(f"color: {COLORS['warning']}")

                        # Champ avec la clé — sélection facile
                        key_input = ui.input(
                            value=raw,
                        ).props("outlined readonly dense").classes("w-full").style(
                            "font-family: monospace; font-size: 13px"
                        )

                        with ui.row().classes("w-full justify-end gap-2 q-mt-lg"):
                            ui.button(
                                "Copier",
                                icon="content_copy",
                                on_click=lambda: (
                                    ui.run_javascript(
                                        f"navigator.clipboard.writeText({raw!r})"
                                        ".then(() => {})"
                                    ),
                                    ui.notify("Clé copiée !", type="positive"),
                                ),
                            ).props("unelevated color=green-8")
                            ui.button(
                                "Fermer",
                                icon="close",
                                on_click=dlg.close,
                            ).props("flat color=grey-7")

                dlg.open()

            ui.button(
                "Générer une clé API",
                icon="add",
                on_click=_generate,
            ).classes("q-mt-sm").props("color=green-8 unelevated")

        with keys_container:
            _build_keys_section(tenant_id, user.get("user_id"))

        # ── Dernière opération : détail produits ─────────────────────
        if ops and ops[0]["status"] in ("pending", "applied"):
            last_op = ops[0]
            section_title("Détail dernière opération", "list_alt")

            # Charger le payload depuis la DB
            from db.conn import run_sql
            import json

            payload_rows = run_sql(
                "SELECT payload FROM sync_operations WHERE id = :id",
                {"id": last_op["id"]},
            )
            if payload_rows:
                payload = payload_rows[0]["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)

                if payload:
                    columns = [
                        {"name": "designation", "label": "Désignation", "field": "designation", "align": "left", "sortable": True},
                        {"name": "marque", "label": "Marque", "field": "marque", "align": "center", "sortable": True},
                        {"name": "code_interne", "label": "Code Interne", "field": "code_interne", "align": "left", "sortable": True},
                        {"name": "pcb", "label": "PCB", "field": "pcb", "align": "center"},
                        {"name": "gtin_uvc", "label": "GTIN UVC", "field": "gtin_uvc", "align": "left"},
                        {"name": "gtin_colis", "label": "GTIN Colis", "field": "gtin_colis", "align": "left"},
                        {"name": "lot", "label": "Lot", "field": "lot_fmt", "align": "center", "sortable": True},
                        {"name": "ddm", "label": "DDM", "field": "ddm_fmt", "align": "center", "sortable": True},
                    ]

                    rows = []
                    for p in payload:
                        lot_raw = p.get("lot", 0)
                        # Formater le lot en string (ex: 10032027)
                        lot_str = str(int(lot_raw)) if lot_raw else ""
                        # Formater la DDM (ISO → DD/MM/YYYY)
                        ddm_raw = p.get("ddm", "")
                        ddm_fmt = ""
                        if ddm_raw and len(ddm_raw) >= 10:
                            try:
                                parts = ddm_raw[:10].split("-")
                                ddm_fmt = f"{parts[2]}/{parts[1]}/{parts[0]}"
                            except (IndexError, ValueError):
                                ddm_fmt = ddm_raw[:10]

                        rows.append({
                            "designation": p.get("designation", ""),
                            "marque": p.get("marque", ""),
                            "code_interne": p.get("code_interne", "") or "—",
                            "pcb": int(p.get("pcb", 0)),
                            "gtin_uvc": p.get("gtin_uvc", ""),
                            "gtin_colis": p.get("gtin_colis", ""),
                            "lot_fmt": lot_str,
                            "ddm_fmt": ddm_fmt,
                        })

                    ui.table(
                        columns=columns,
                        rows=rows,
                        row_key="designation",
                    ).props("flat bordered dense").classes("w-full")
