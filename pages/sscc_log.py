"""
pages/sscc_log.py
=================
Page d'audit du journal SSCC palette — accessible uniquement aux admins.

Pour les besoins :
  - **Traçabilité réglementaire** (BIO, IFS, BRC) : retrouver toutes les
    palettes générées pour un lot donné en cas de rappel produit.
  - **Audit interne** : visualiser l'historique d'émission des SSCC, par
    qui, quand, pour quel produit / lot.
  - **Export CSV** : transmettre aux auditeurs ou aux clients.

UI :
  - Cards stats en haut (aujourd'hui / ce mois / total)
  - Filtres : date_from, date_to, lot
  - Bouton Export CSV (téléchargement direct)
  - Table triée par date descendante (le plus récent en haut), max 500 lignes
"""
from __future__ import annotations

import asyncio
import csv
import datetime as _dt
import io
import logging

from nicegui import ui

from common.services.sscc_service import (
    SsccLogEntry,
    get_sscc_stats,
    list_sscc_log,
)
from pages.auth import require_auth
from pages.theme import COLORS, page_layout, section_title

_log = logging.getLogger("ferment.sscc_log")


def _require_admin() -> dict | None:
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


def _fmt_sscc(s: str) -> str:
    """Formate un SSCC 18 digits en groupes pour la lecture humaine."""
    if not s or len(s) != 18:
        return s or ""
    return f"{s[0:4]} {s[4:8]} {s[8:12]} {s[12:16]} {s[16:18]}"


def _build_csv(entries: list[SsccLogEntry]) -> bytes:
    """Construit un CSV UTF-8 BOM (compatible Excel)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    w.writerow([
        "Date", "Heure", "SSCC", "GTIN palette", "Lot", "DDM",
        "Nb cartons", "Utilisateur",
        "Statut", "Ramasse date", "Ramasse destinataire",
    ])
    for e in entries:
        when = e.generated_at
        date_str = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when)
        time_str = when.strftime("%H:%M:%S") if hasattr(when, "strftime") else ""
        ddm_str = e.ddm.strftime("%Y-%m-%d") if e.ddm else ""
        if e.voided_at:
            statut = f"Annulé ({e.voided_reason or '?'})"
        elif e.ramasse_id:
            statut = "Chargée"
        else:
            statut = "En stock"
        ramasse_date_str = e.ramasse_date.strftime("%Y-%m-%d") if e.ramasse_date else ""
        w.writerow([
            date_str, time_str, e.sscc, e.gtin_palette,
            e.lot, ddm_str, str(e.case_count), e.user_email,
            statut, ramasse_date_str, e.ramasse_destinataire,
        ])
    # BOM UTF-8 pour qu'Excel détecte l'encodage correctement
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


@ui.page("/sscc-log")
def page_sscc_log():
    user = _require_admin()
    if not user:
        return

    tenant_id = user.get("tenant_id", "")

    # State partagé entre les handlers
    state: dict = {
        "date_from": None,
        "date_to": None,
        "lot": "",
    }

    with page_layout("Journal SSCC", "history", "/sscc-log"):
        ui.label(
            "Traçabilité GS1 — historique de tous les SSCC palette générés. "
            "Utile pour audits BIO/IFS, rappels produit, contrôles internes."
        ).classes("text-body2").style(f"color: {COLORS['ink2']}")

        # ── Stats en haut ─────────────────────────────────────────────
        stats_row = ui.row().classes("w-full gap-3 q-mt-md")
        stat_today_lbl = ui.label("—")
        stat_month_lbl = ui.label("—")
        stat_total_lbl = ui.label("—")

        def _stat_card(parent, value_label: ui.label, title: str, color: str):
            with parent:
                with ui.card().classes("flex-1").props("flat bordered").style(
                    "min-width: 140px",
                ):
                    with ui.card_section().classes("q-pa-md"):
                        value_label.classes("text-h4").style(
                            f"color: {color}; font-weight: 700",
                        )
                        ui.label(title).classes("text-caption").style(
                            f"color: {COLORS['ink2']}; letter-spacing: 1px",
                        )

        # Reconstruct stats UI in the row (cleaner than parent= magic)
        with stats_row:
            with ui.card().classes("flex-1").props("flat bordered").style("min-width: 140px"):
                with ui.card_section().classes("q-pa-md"):
                    stat_today_lbl = ui.label("—").classes("text-h4").style(
                        f"color: {COLORS['green']}; font-weight: 700",
                    )
                    ui.label("AUJOURD'HUI").classes("text-caption").style(
                        f"color: {COLORS['ink2']}; letter-spacing: 1px",
                    )
            with ui.card().classes("flex-1").props("flat bordered").style("min-width: 140px"):
                with ui.card_section().classes("q-pa-md"):
                    stat_month_lbl = ui.label("—").classes("text-h4").style(
                        f"color: {COLORS['blue']}; font-weight: 700",
                    )
                    ui.label("CE MOIS").classes("text-caption").style(
                        f"color: {COLORS['ink2']}; letter-spacing: 1px",
                    )
            with ui.card().classes("flex-1").props("flat bordered").style("min-width: 140px"):
                with ui.card_section().classes("q-pa-md"):
                    stat_total_lbl = ui.label("—").classes("text-h4").style(
                        f"color: {COLORS['ink']}; font-weight: 700",
                    )
                    ui.label("TOTAL").classes("text-caption").style(
                        f"color: {COLORS['ink2']}; letter-spacing: 1px",
                    )

        # ── Filtres ───────────────────────────────────────────────────
        section_title("Filtres", "filter_alt")
        with ui.card().classes("w-full q-pa-md").props("flat bordered"):
            with ui.row().classes("w-full gap-3 items-end"):
                date_from_input = ui.input(
                    label="Date depuis (YYYY-MM-DD)",
                    placeholder="2026-05-01",
                ).classes("flex-1").props("outlined dense")
                date_to_input = ui.input(
                    label="Date jusqu'à (YYYY-MM-DD)",
                    placeholder="2026-05-31",
                ).classes("flex-1").props("outlined dense")
                lot_input = ui.input(
                    label="Filtre lot (contient)",
                    placeholder="ex: 08052027",
                ).classes("flex-1").props("outlined dense")

            with ui.row().classes("w-full gap-2 q-mt-sm"):
                apply_btn = ui.button("Appliquer", icon="search").props(
                    "color=green-8 unelevated",
                )
                reset_btn = ui.button("Réinitialiser", icon="refresh").props(
                    "outline color=grey-7",
                )
                export_btn = ui.button("Exporter CSV", icon="download").props(
                    "outline color=blue-7",
                )

        # ── Table ─────────────────────────────────────────────────────
        section_title("Résultats", "list_alt")
        table_caption = ui.label("").classes("text-caption q-mb-xs").style(
            f"color: {COLORS['ink2']}",
        )
        table_container = ui.column().classes("w-full")

        # ── Logique réactive ──────────────────────────────────────────

        def _parse_date(s: str) -> _dt.date | None:
            s = (s or "").strip()
            if not s:
                return None
            try:
                return _dt.date.fromisoformat(s)
            except ValueError:
                ui.notify(f"Date invalide : {s} (format YYYY-MM-DD)", type="warning")
                return None

        def _refresh_stats():
            stats = get_sscc_stats(tenant_id)
            stat_today_lbl.text = str(stats["today"])
            stat_month_lbl.text = str(stats["this_month"])
            stat_total_lbl.text = str(stats["total"])

        def _refresh_table():
            entries = list_sscc_log(
                tenant_id,
                date_from=state["date_from"],
                date_to=state["date_to"],
                lot_filter=state["lot"],
                limit=500,
            )
            table_container.clear()
            if not entries:
                with table_container:
                    ui.label("Aucun SSCC ne correspond aux filtres.").classes(
                        "text-body2 q-pa-md",
                    ).style(f"color: {COLORS['ink2']}; font-style: italic")
                table_caption.text = "0 résultat"
                state["_entries"] = []
                return

            # Map sscc → entry pour le callback de void
            by_sscc = {e.sscc: e for e in entries}

            rows = []
            for e in entries:
                is_voided = bool(e.voided_at)
                is_loaded = bool(e.ramasse_id)
                # Colonne "Ramasse" : date + destinataire si lié, sinon "—"
                if is_loaded and e.ramasse_date:
                    ramasse_str = (
                        f"{e.ramasse_date.strftime('%d/%m/%Y')} · "
                        f"{e.ramasse_destinataire or '?'}"
                    )
                else:
                    ramasse_str = "— en stock —"
                rows.append({
                    "datetime": e.generated_at.strftime("%d/%m/%Y %H:%M:%S")
                        if hasattr(e.generated_at, "strftime") else str(e.generated_at),
                    "sscc": _fmt_sscc(e.sscc),
                    "sscc_raw": e.sscc,  # pour le callback
                    "gtin": e.gtin_palette or "—",
                    "lot": e.lot or "—",
                    "ddm": e.ddm.strftime("%d/%m/%Y") if e.ddm else "—",
                    "cartons": e.case_count,
                    "ramasse": ramasse_str,
                    "user": e.user_email or "—",
                    "voided": is_voided,
                    "voided_reason": e.voided_reason or "",
                    "loaded": is_loaded,
                })
            cols = [
                {"name": "datetime", "label": "Date / Heure", "field": "datetime",
                 "align": "left", "sortable": True},
                {"name": "sscc", "label": "SSCC", "field": "sscc", "align": "left",
                 "sortable": True, "classes": "monospace-cell"},
                {"name": "gtin", "label": "GTIN palette", "field": "gtin",
                 "align": "left", "sortable": True},
                {"name": "lot", "label": "Lot", "field": "lot", "align": "left",
                 "sortable": True},
                {"name": "ddm", "label": "DDM", "field": "ddm", "align": "left",
                 "sortable": True},
                {"name": "cartons", "label": "Cartons", "field": "cartons",
                 "align": "right", "sortable": True},
                {"name": "ramasse", "label": "Ramasse", "field": "ramasse",
                 "align": "left", "sortable": True},
                {"name": "user", "label": "Utilisateur", "field": "user",
                 "align": "left", "sortable": True},
                {"name": "action", "label": "", "field": "sscc_raw",
                 "align": "center"},
            ]
            with table_container:
                table = ui.table(
                    columns=cols, rows=rows, row_key="sscc_raw",
                    pagination={"rowsPerPage": 25},
                ).classes("w-full").props("flat bordered dense")

                # Slot custom : ligne grisée si annulée, cellule ramasse
                # colorée selon chargée (vert) / en stock (gris).
                table.add_slot("body", """
                    <q-tr :props="props" :style="props.row.voided ?
                        'opacity: 0.5; text-decoration: line-through' : ''">
                        <q-td v-for="col in props.cols" :key="col.name" :props="props">
                            <template v-if="col.name === 'action'">
                                <q-btn v-if="!props.row.voided"
                                       flat dense color="red-7" icon="block"
                                       label="Annuler"
                                       @click="$parent.$emit('void_sscc', props.row.sscc_raw)" />
                                <span v-else style="font-size: 10px; color: #888">
                                    {{ props.row.voided_reason || 'annulée' }}
                                </span>
                            </template>
                            <template v-else-if="col.name === 'ramasse'">
                                <span :style="props.row.loaded ?
                                    'color: #15803D; font-weight: 500' : 'color: #999; font-style: italic'">
                                    {{ col.value }}
                                </span>
                            </template>
                            <template v-else>
                                {{ col.value }}
                            </template>
                        </q-td>
                    </q-tr>
                """)

                def _on_void_sscc(e):
                    sscc = str(e.args) if e.args else ""
                    entry = by_sscc.get(sscc)
                    if entry is None:
                        return
                    _open_void_dialog_admin(entry)

                table.on("void_sscc", _on_void_sscc)

            n = len(entries)
            cap = f"{n} résultat" + ("s" if n > 1 else "")
            if n >= 500:
                cap += " (limite atteinte — affine les filtres)"
            table_caption.text = cap
            state["_entries"] = entries

        def _open_void_dialog_admin(entry):
            """Dialog d'annulation côté admin — même UX que la page
            étiquettes-palette mais réutilisable ici."""
            with ui.dialog() as dlg, ui.card().classes("q-pa-md").style("min-width: 380px"):
                ui.label("Annuler ce SSCC ?").classes("text-h6").style(
                    f"color: {COLORS['ink']}; font-weight: 700",
                )
                ui.label(f"SSCC : {_fmt_sscc(entry.sscc)}").classes(
                    "text-caption q-mb-xs",
                ).style(f"font-family: monospace; color: {COLORS['ink']}")
                ui.label(
                    f"GTIN {entry.gtin_palette} · Lot {entry.lot} · {entry.case_count}c",
                ).classes("text-caption q-mb-md").style(f"color: {COLORS['ink2']}")
                ui.label(
                    "Le séquentiel reste consommé (norme GS1). La palette "
                    "ne sera plus proposée au chargement.",
                ).classes("text-caption q-mb-md").style(f"color: {COLORS['ink2']}")
                reason_input = ui.input(
                    label="Raison",
                    placeholder="ex: étiquette pas imprimée, doublon",
                ).classes("w-full").props("outlined dense autofocus")

                async def _submit():
                    reason = (reason_input.value or "").strip()
                    if not reason:
                        ui.notify("Saisis une raison.", type="warning")
                        return
                    dlg.close()
                    from common.services.sscc_service import void_sscc
                    ok = await asyncio.to_thread(
                        void_sscc, tenant_id, entry.sscc,
                        reason=reason, user_email=user.get("email", ""),
                    )
                    if ok:
                        ui.notify(
                            f"✓ SSCC {_fmt_sscc(entry.sscc)} annulé.",
                            type="positive", icon="block",
                        )
                        _refresh_table()
                        _refresh_stats()
                    else:
                        ui.notify(
                            "Annulation impossible.", type="warning",
                        )

                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Annuler", on_click=dlg.close).props("flat color=grey-7")
                    ui.button(
                        "Confirmer", icon="block", on_click=_submit,
                    ).props("color=red-7 unelevated")
            dlg.open()

        def _apply_filters():
            state["date_from"] = _parse_date(date_from_input.value or "")
            state["date_to"] = _parse_date(date_to_input.value or "")
            state["lot"] = (lot_input.value or "").strip()
            _refresh_table()

        def _reset_filters():
            date_from_input.value = ""
            date_to_input.value = ""
            lot_input.value = ""
            state["date_from"] = None
            state["date_to"] = None
            state["lot"] = ""
            _refresh_table()

        def _export_csv():
            entries = state.get("_entries") or []
            if not entries:
                ui.notify("Aucun résultat à exporter.", type="warning")
                return
            data = _build_csv(entries)
            fname = f"sscc_log_{_dt.date.today().strftime('%Y%m%d')}.csv"
            ui.download(data, fname)
            ui.notify(
                f"✓ Export CSV : {len(entries)} ligne(s).",
                type="positive", icon="download",
            )

        apply_btn.on_click(_apply_filters)
        reset_btn.on_click(_reset_filters)
        export_btn.on_click(_export_csv)

        # Charge initiale
        _refresh_stats()
        _refresh_table()
