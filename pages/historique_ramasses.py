"""
pages/historique_ramasses.py
============================
Page d'historique des ramasses + corbeille — vue complète et actions.

Vit en pendant de /chargement-camion qui propose une vue courte (5
dernières) avec un lien vers cette page. La page ici offre :

- Tableau Quasar paginé (20/page) avec colonnes date, destinataire,
  statut (PRÉV / DÉF / LEGACY), palettes, cartons, livré, actions.
- Actions sur chaque ligne : télécharger PDF, renvoyer par email,
  marquer chauffeur passé / annuler livraison, supprimer (soft-delete
  → corbeille 7j).
- Section corbeille repliable : ramasses soft-deleted récupérables.
- Bouton export CSV (500 dernières, corbeille incluse).

La logique métier vient de ``common/ramasse_history`` (déjà testée),
cette page n'est que de l'orchestration UI.
"""
from __future__ import annotations

import asyncio
import logging

from nicegui import ui

from common.email import send_html_with_pdf
from common.ramasse import today_paris
from common.ramasse_export import build_csv_bytes
from common.ramasse_history import (
    count_deleted_ramasses,
    count_ramasses,
    delete_ramasse,
    get_ramasse,
    list_deleted_ramasses,
    list_ramasses,
    mark_driver_passed,
    restore_ramasse,
    unmark_driver_passed,
)
from pages.auth import require_auth
from pages.theme import COLORS, page_layout, section_title

_log = logging.getLogger("ferment.historique_ramasses")


_STATUS_LABELS = {
    "previsionnel": ("PRÉV", "blue-7"),
    "definitif":    ("DÉF", "green-8"),
    "legacy":       ("LEGACY", "grey-6"),
    "sent":         ("LEGACY", "grey-6"),  # ancien défaut historique
}


@ui.page("/historique-ramasses")
def page_historique_ramasses():
    user = require_auth()
    if not user:
        return
    tenant_id = user.get("tenant_id", "")

    with page_layout(
        "Historique des ramasses", "history", "/historique-ramasses",
    ):
        ui.label(
            "Toutes les fiches de ramasse envoyées. Téléchargement PDF, "
            "renvoi, verrouillage chauffeur et corbeille (récupération 7j).",
        ).classes("text-body2").style(f"color: {COLORS['ink2']}")

        # Container principal — reconstruit après chaque action pour
        # refléter l'état DB courant.
        list_container = ui.column().classes("w-full q-mt-md")

        def _refresh():
            list_container.clear()
            with list_container:
                _render_main_table(tenant_id)
                _render_trash_section(tenant_id, on_refresh=_refresh)

        _refresh()


# ─── Tableau principal ──────────────────────────────────────────────────────

def _render_main_table(tenant_id: str) -> None:
    """Rend le tableau des ramasses actives (non-corbeille)."""
    try:
        total = count_ramasses(tenant_id=tenant_id)
    except Exception:
        _log.warning("count_ramasses échec", exc_info=True)
        total = 0

    with ui.row().classes("w-full items-center justify-between q-mb-sm"):
        section_title(f"Ramasses ({total})", "list_alt")
        ui.button(
            "Exporter CSV", icon="file_download",
            on_click=lambda: _export_csv(tenant_id),
        ).props("flat dense color=grey-7").tooltip(
            "Télécharge les 500 dernières (corbeille incluse)",
        )

    if total == 0:
        ui.label("Aucune ramasse enregistrée.").classes(
            "text-grey-6 q-pa-md",
        )
        return

    try:
        items = list_ramasses(tenant_id=tenant_id, limit=100)
    except Exception:
        _log.warning("list_ramasses échec", exc_info=True)
        ui.label("Erreur de chargement.").classes("text-negative")
        return

    cols = [
        {"name": "date", "label": "Date", "field": "date",
         "align": "left", "sortable": True},
        {"name": "dest", "label": "Destinataire", "field": "dest",
         "align": "left"},
        {"name": "statut", "label": "Statut", "field": "statut",
         "align": "center"},
        {"name": "palettes", "label": "Palettes", "field": "palettes",
         "align": "right", "sortable": True},
        {"name": "cartons", "label": "Cartons", "field": "cartons",
         "align": "right", "sortable": True},
        {"name": "livre", "label": "Livré", "field": "livre",
         "align": "center"},
        {"name": "actions", "label": "", "field": "actions",
         "align": "center"},
    ]

    rows = []
    for item in items:
        dr = item.get("date_ramasse")
        date_str = dr.strftime("%d/%m/%Y") if hasattr(dr, "strftime") else str(dr)
        status = str(item.get("status") or "")
        rows.append({
            "id": str(item["id"]),
            "date": date_str,
            "dest": item.get("destinataire", ""),
            "palettes": item.get("total_palettes", 0),
            "cartons": item.get("total_cartons", 0),
            "_status": status,
            "driver_passed": bool(item.get("driver_passed", False)),
            "version": int(item.get("version") or 1),
            "statut": "",
            "livre": "",
            "actions": "",
        })

    ht = ui.table(
        columns=cols, rows=rows, row_key="id",
        pagination={"rowsPerPage": 20},
    ).classes("w-full").props("flat bordered dense")

    # Slot statut : badge coloré selon _status
    ht.add_slot("body-cell-statut", r"""
        <q-td :props="props" style="text-align: center">
            <q-badge
                :color="props.row._status === 'definitif' ? 'green-8'
                      : props.row._status === 'previsionnel' ? 'blue-7'
                      : 'grey-6'"
                style="font-size: 11px; padding: 2px 6px">
                {{
                    props.row._status === 'definitif' ? 'DÉF' :
                    props.row._status === 'previsionnel' ? 'PRÉV' :
                    'LEGACY'
                }}
                <span v-if="props.row.version > 1" style="margin-left:4px; opacity:0.85">
                    v{{ props.row.version }}
                </span>
            </q-badge>
        </q-td>
    """)

    # Slot livré : icône chauffeur passé
    ht.add_slot("body-cell-livre", r"""
        <q-td :props="props" style="text-align: center">
            <q-icon v-if="props.row.driver_passed"
                    name="local_shipping" size="sm" color="green-7">
                <q-tooltip>Livrée — verrouillée</q-tooltip>
            </q-icon>
            <span v-else class="text-grey-5">—</span>
        </q-td>
    """)

    # Slot actions
    ht.add_slot("body-cell-actions", r"""
        <q-td :props="props" style="text-align: center; white-space: nowrap">
            <q-btn flat round dense icon="picture_as_pdf" size="sm" color="green-8"
                @click="() => $parent.$emit('dl', props.row.id)">
                <q-tooltip>Télécharger le PDF</q-tooltip>
            </q-btn>
            <q-btn flat round dense icon="forward_to_inbox" size="sm" color="blue-8"
                @click="() => $parent.$emit('resend', props.row.id)">
                <q-tooltip>Renvoyer par email</q-tooltip>
            </q-btn>
            <q-btn v-if="!props.row.driver_passed"
                flat round dense icon="local_shipping" size="sm" color="green-7"
                @click="() => $parent.$emit('mark', props.row.id)">
                <q-tooltip>Chauffeur passé — verrouiller</q-tooltip>
            </q-btn>
            <q-btn v-else
                flat round dense icon="lock_open" size="sm" color="orange-8"
                @click="() => $parent.$emit('unmark', {id: props.row.id, date: props.row.date, dest: props.row.dest})">
                <q-tooltip>Annuler la livraison (déverrouille)</q-tooltip>
            </q-btn>
            <q-btn flat round dense icon="delete" size="sm" color="red-7"
                @click="() => $parent.$emit('del', {id: props.row.id, date: props.row.date, dest: props.row.dest})">
                <q-tooltip>Supprimer (corbeille 7j)</q-tooltip>
            </q-btn>
        </q-td>
    """)

    ht.on("dl",     lambda e: asyncio.create_task(_on_download_pdf(e.args, tenant_id)))
    ht.on("resend", lambda e: asyncio.create_task(_on_resend(e.args, tenant_id)))
    ht.on("mark",   lambda e: _open_mark_dialog(e.args, tenant_id))
    ht.on("unmark", lambda e: _open_unmark_dialog(e.args, tenant_id))
    ht.on("del",    lambda e: _open_delete_dialog(e.args, tenant_id))


# ─── Corbeille ──────────────────────────────────────────────────────────────

def _render_trash_section(tenant_id: str, *, on_refresh) -> None:
    try:
        n = count_deleted_ramasses(tenant_id=tenant_id)
    except Exception:
        _log.warning("count_deleted_ramasses échec", exc_info=True)
        n = 0
    if n == 0:
        return

    with ui.expansion(
        text=f"Corbeille ({n})",
        icon="delete_outline",
        value=False,
    ).classes("w-full q-mt-md").props(
        "dense header-class='text-body2 text-grey-7'",
    ).style(
        f"border: 1px dashed {COLORS.get('border', '#E5E7EB')}; border-radius: 8px",
    ):
        ui.label(
            "Ramasses supprimées récupérables 7 jours. "
            "Au-delà, suppression définitive automatique.",
        ).classes("text-caption text-grey-6 q-px-sm q-pb-xs")
        try:
            deleted = list_deleted_ramasses(tenant_id=tenant_id, limit=50)
        except Exception:
            _log.warning("list_deleted_ramasses échec", exc_info=True)
            ui.label("Erreur de chargement.").classes("text-negative")
            return
        for item in deleted:
            rid = str(item["id"])
            dr = item.get("date_ramasse")
            date_str = dr.strftime("%d/%m/%Y") if hasattr(dr, "strftime") else str(dr)
            del_at = item.get("deleted_at")
            del_str = (
                del_at.strftime("%d/%m %H:%M") if hasattr(del_at, "strftime") else "?"
            )
            with ui.row().classes(
                "w-full items-center gap-3 q-px-sm q-py-xs",
            ).style("border-top: 1px solid #F3F4F6"):
                ui.icon("delete_outline", size="sm", color="grey-6")
                with ui.column().classes("flex-1 gap-0"):
                    ui.label(
                        f"{date_str} — {item.get('destinataire', '?')}",
                    ).classes("text-body2").style(
                        f"color: {COLORS['ink']}; font-weight: 500",
                    )
                    ui.label(
                        f"{item.get('total_palettes', 0)} pal · "
                        f"{item.get('total_cartons', 0)} cartons · "
                        f"supprimée le {del_str}",
                    ).classes("text-caption text-grey-6")

                async def _do_restore(_=None, _rid=rid):
                    try:
                        ok = await asyncio.to_thread(
                            restore_ramasse, _rid, tenant_id=tenant_id,
                        )
                    except Exception:
                        _log.warning("restore_ramasse échec", exc_info=True)
                        ui.notify("Erreur lors de la restauration.", type="negative")
                        return
                    if ok:
                        ui.notify("Ramasse restaurée ✓", type="positive", icon="restore")
                        on_refresh()
                    else:
                        ui.notify(
                            "Restauration impossible (introuvable ou déjà purgée).",
                            type="warning",
                        )

                ui.button(
                    "Restaurer", icon="restore", on_click=_do_restore,
                ).props("dense flat color=blue-8").classes("q-px-sm")


# ─── Actions (dialogs + handlers async) ─────────────────────────────────────

async def _on_download_pdf(rid: str, tenant_id: str) -> None:
    try:
        rec = await asyncio.to_thread(get_ramasse, rid, tenant_id=tenant_id)
    except Exception:
        _log.warning("get_ramasse échec", exc_info=True)
        ui.notify("Erreur de chargement.", type="negative")
        return
    if not rec or not rec.get("pdf_bytes"):
        ui.notify("PDF non disponible.", type="warning")
        return
    pdf = rec["pdf_bytes"]
    if isinstance(pdf, memoryview):
        pdf = bytes(pdf)
    dr = rec.get("date_ramasse")
    fname = f"Ramasse_{dr}.pdf" if dr else "Ramasse.pdf"
    ui.download(pdf, fname)


async def _on_resend(rid: str, tenant_id: str) -> None:
    try:
        rec = await asyncio.to_thread(get_ramasse, rid, tenant_id=tenant_id)
    except Exception:
        _log.warning("get_ramasse échec", exc_info=True)
        ui.notify("Erreur de chargement.", type="negative")
        return
    if not rec or not rec.get("pdf_bytes"):
        ui.notify("PDF non disponible.", type="warning")
        return
    recipients = rec.get("recipients") or []
    if not recipients:
        ui.notify("Aucun destinataire enregistré.", type="warning")
        return
    pdf = rec["pdf_bytes"]
    if isinstance(pdf, memoryview):
        pdf = bytes(pdf)
    dr = rec.get("date_ramasse")
    fname = f"Ramasse_{dr}.pdf" if dr else "Ramasse.pdf"
    subject = f"Renvoi ramasse {dr} — Ferment Station"
    body = (
        "<p>Bonjour,</p>"
        "<p>Ci-joint le renvoi de la fiche de ramasse.</p>"
        "<p>Cordialement,<br>Ferment Station</p>"
    )
    try:
        await asyncio.to_thread(
            send_html_with_pdf,
            to_email=recipients, subject=subject,
            html_body=body, attachments=[(fname, pdf)],
        )
    except Exception as exc:
        _log.exception("Renvoi échec")
        ui.notify(f"Erreur envoi : {exc}", type="negative")
        return
    ui.notify(
        f"Ramasse renvoyée à {len(recipients)} destinataire(s).",
        type="positive", icon="email",
    )


def _open_mark_dialog(rid: str, tenant_id: str) -> None:
    with ui.dialog() as dlg, ui.card():
        ui.label("Marquer comme livrée ?").classes("text-h6")
        ui.label(
            "Cette ramasse sera verrouillée et ne pourra plus être modifiée.",
        ).classes("text-body2 text-grey-7")
        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Annuler", on_click=dlg.close).props("flat")

            async def _confirm():
                dlg.close()
                try:
                    ok = await asyncio.to_thread(
                        mark_driver_passed, rid, tenant_id=tenant_id,
                    )
                except Exception:
                    _log.warning("mark_driver_passed échec", exc_info=True)
                    ui.notify("Erreur.", type="negative")
                    return
                if ok:
                    ui.notify("Ramasse marquée livrée.",
                              type="positive", icon="local_shipping")
                    ui.navigate.reload()
                else:
                    ui.notify("Déjà livrée ou introuvable.", type="warning")

            ui.button(
                "Confirmer", icon="local_shipping", on_click=_confirm,
            ).props("color=green-7 unelevated")
    dlg.open()


def _open_unmark_dialog(args: dict, tenant_id: str) -> None:
    rid = args.get("id", "")
    date_str = args.get("date", "?")
    dest = args.get("dest", "?")
    with ui.dialog() as dlg, ui.card():
        ui.label("Annuler la livraison ?").classes("text-h6")
        ui.label(f"Ramasse du {date_str} — {dest}").classes("text-body2").style(
            f"color: {COLORS['ink']}; font-weight: 500",
        )
        ui.label(
            "La fiche redevient modifiable.",
        ).classes("text-caption q-mt-sm").style(
            f"color: {COLORS.get('ink2', '#6B7280')}",
        )
        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Retour", on_click=dlg.close).props("flat")

            async def _confirm():
                dlg.close()
                try:
                    ok = await asyncio.to_thread(
                        unmark_driver_passed, rid, tenant_id=tenant_id,
                    )
                except Exception:
                    _log.warning("unmark_driver_passed échec", exc_info=True)
                    ui.notify("Erreur.", type="negative")
                    return
                if ok:
                    ui.notify("Livraison annulée.",
                              type="positive", icon="lock_open")
                    ui.navigate.reload()
                else:
                    ui.notify("Introuvable ou déjà non-livrée.", type="warning")

            ui.button(
                "Annuler la livraison", icon="lock_open", on_click=_confirm,
            ).props("color=orange-8 unelevated")
    dlg.open()


def _open_delete_dialog(args: dict, tenant_id: str) -> None:
    rid = args.get("id", "")
    date_str = args.get("date", "?")
    dest = args.get("dest", "?")
    with ui.dialog() as dlg, ui.card():
        ui.label("Supprimer cette ramasse ?").classes("text-h6")
        ui.label(f"Ramasse du {date_str} — {dest}").classes("text-body2").style(
            f"color: {COLORS['ink']}; font-weight: 500",
        )
        ui.label(
            "Déplacée dans la corbeille et récupérable 7 jours. "
            "Au-delà, suppression définitive automatique.",
        ).classes("text-caption q-mt-sm").style(
            f"color: {COLORS.get('ink2', '#6B7280')}",
        )
        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Annuler", on_click=dlg.close).props("flat")

            async def _confirm():
                dlg.close()
                try:
                    ok = await asyncio.to_thread(
                        delete_ramasse, rid, tenant_id=tenant_id,
                    )
                except Exception:
                    _log.warning("delete_ramasse échec", exc_info=True)
                    ui.notify("Erreur.", type="negative")
                    return
                if ok:
                    ui.notify(
                        "Ramasse en corbeille (récupérable 7j).",
                        type="positive", icon="delete",
                    )
                    ui.navigate.reload()
                else:
                    ui.notify("Introuvable.", type="warning")

            ui.button(
                "Supprimer", icon="delete", on_click=_confirm,
            ).props("color=red-7 unelevated")
    dlg.open()


# ─── Export CSV ─────────────────────────────────────────────────────────────

def _export_csv(tenant_id: str) -> None:
    """Télécharge un CSV des 500 dernières ramasses (corbeille incluse)."""
    try:
        items = list_ramasses(tenant_id=tenant_id, limit=500, include_deleted=True)
    except Exception:
        _log.warning("list_ramasses (export) échec", exc_info=True)
        ui.notify("Erreur de chargement.", type="negative")
        return
    if not items:
        ui.notify("Aucune ramasse à exporter.", type="info")
        return
    try:
        csv_bytes = build_csv_bytes(items)
    except Exception:
        _log.warning("build_csv_bytes échec", exc_info=True)
        ui.notify("Erreur génération CSV.", type="negative")
        return
    today_str = today_paris().strftime("%Y-%m-%d")
    ui.download(
        csv_bytes,
        filename=f"ramasses_export_{today_str}.csv",
        media_type="text/csv; charset=utf-8",
    )
    ui.notify(
        f"Export CSV ({len(items)} ramasses).",
        type="positive", icon="file_download",
    )
