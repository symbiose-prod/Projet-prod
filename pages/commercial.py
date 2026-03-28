"""
pages/commercial.py
===================
Dashboard commercial — Comparatif CA mensuel 2025 vs 2026.
"""
from __future__ import annotations

import asyncio
import logging

from nicegui import ui

from pages.auth import require_auth
from pages.theme import COLORS, page_layout, section_title

_log = logging.getLogger("ferment.commercial")


def _fmt_eur(v: float) -> str:
    """Formate un montant en euros : 12 345 €."""
    if v == 0:
        return "—"
    return f"{v:,.0f} €".replace(",", " ")


def _fmt_pct(v: float) -> str:
    """Formate un pourcentage avec signe."""
    if v == 0:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f} %"


def _pct_color(v: float) -> str:
    """Couleur du pourcentage : vert si positif, rouge si négatif."""
    if v > 0:
        return COLORS["green"]
    if v < 0:
        return COLORS["error"]
    return COLORS["ink2"]


@ui.page("/commercial")
async def page_commercial():
    user = require_auth()
    if not user:
        return

    with page_layout("Dashboard Commercial", "bar_chart", "/commercial"):

        # ── Chargement ──
        with ui.column().classes("w-full items-center q-pa-xl"):
            spinner = ui.spinner("dots", size="lg", color="green")
            status_label = ui.label("Chargement des données CA depuis EasyBeer...").classes(
                "text-caption text-grey-6"
            )

        try:
            from pages._commercial_calc import fetch_ca_comparison

            result = await asyncio.to_thread(fetch_ca_comparison, 2025, 2026)
        except Exception as exc:
            _log.exception("Erreur chargement CA")
            spinner.visible = False
            status_label.text = f"Erreur : {exc}"
            status_label.classes("text-negative")
            return

        spinner.delete()
        status_label.delete()

        year_a = result["year_a"]
        year_b = result["year_b"]
        months = result["months"]
        ytd_a = result["ytd_a"]
        ytd_b = result["ytd_b"]
        ytd_pct = result["ytd_pct"]
        current_month = result["current_month"]

        # ── KPIs YTD ──
        section_title(f"Cumul à date (janvier → {months[current_month - 1]['label'].lower()})", "trending_up")

        with ui.row().classes("w-full gap-4 q-mb-md"):
            # CA YTD année A
            with ui.card().classes("flex-1 q-pa-none").props("flat"):
                with ui.card_section().classes("row items-center gap-3 q-pa-md"):
                    with ui.element("div").classes("q-pa-xs").style(
                        f"background: {COLORS['ink2']}15; border-radius: 6px"
                    ):
                        ui.icon("calendar_month", size="sm").style(f"color: {COLORS['ink2']}")
                    with ui.column().classes("gap-0"):
                        ui.label(f"CA {year_a} (YTD)").classes("text-caption").style(
                            f"color: {COLORS['ink2']}; font-weight: 500"
                        )
                        ui.label(_fmt_eur(ytd_a)).classes("text-h6").style(
                            f"color: {COLORS['ink']}; font-weight: 600"
                        )

            # CA YTD année B
            with ui.card().classes("flex-1 q-pa-none").props("flat"):
                with ui.card_section().classes("row items-center gap-3 q-pa-md"):
                    with ui.element("div").classes("q-pa-xs").style(
                        f"background: {COLORS['green']}15; border-radius: 6px"
                    ):
                        ui.icon("calendar_month", size="sm").style(f"color: {COLORS['green']}")
                    with ui.column().classes("gap-0"):
                        ui.label(f"CA {year_b} (YTD)").classes("text-caption").style(
                            f"color: {COLORS['ink2']}; font-weight: 500"
                        )
                        ui.label(_fmt_eur(ytd_b)).classes("text-h6").style(
                            f"color: {COLORS['ink']}; font-weight: 600"
                        )

            # Évolution YTD
            with ui.card().classes("flex-1 q-pa-none").props("flat"):
                with ui.card_section().classes("row items-center gap-3 q-pa-md"):
                    pct_col = _pct_color(ytd_pct)
                    with ui.element("div").classes("q-pa-xs").style(
                        f"background: {pct_col}15; border-radius: 6px"
                    ):
                        icon_name = "trending_up" if ytd_pct >= 0 else "trending_down"
                        ui.icon(icon_name, size="sm").style(f"color: {pct_col}")
                    with ui.column().classes("gap-0"):
                        ui.label("Évolution YTD").classes("text-caption").style(
                            f"color: {COLORS['ink2']}; font-weight: 500"
                        )
                        ui.label(_fmt_pct(ytd_pct)).classes("text-h6").style(
                            f"color: {pct_col}; font-weight: 600"
                        )

        # ── Tableau mensuel ──
        section_title(f"CA mensuel {year_a} vs {year_b}", "table_chart")

        columns = [
            {"name": "label", "label": "Mois", "field": "label", "align": "left", "sortable": False},
            {"name": "ca_a", "label": f"CA {year_a} (€)", "field": "ca_a", "align": "right", "sortable": False},
            {"name": "ca_b", "label": f"CA {year_b} (€)", "field": "ca_b", "align": "right", "sortable": False},
            {"name": "pct", "label": "Évolution", "field": "pct", "align": "right", "sortable": False},
        ]

        rows = []
        for m in months:
            is_future = m["month"] > current_month
            rows.append({
                "label": m["label"],
                "ca_a": _fmt_eur(m["ca_a"]),
                "ca_b": _fmt_eur(m["ca_b"]) if not is_future else "—",
                "pct": _fmt_pct(m["pct"]) if not is_future and (m["ca_a"] > 0 or m["ca_b"] > 0) else "—",
                "_pct_raw": m["pct"],
                "_is_future": is_future,
            })

        # Ligne totale YTD
        rows.append({
            "label": f"TOTAL YTD (→ {months[current_month - 1]['label']})",
            "ca_a": _fmt_eur(ytd_a),
            "ca_b": _fmt_eur(ytd_b),
            "pct": _fmt_pct(ytd_pct),
            "_pct_raw": ytd_pct,
            "_is_future": False,
            "_is_total": True,
        })

        table = ui.table(
            columns=columns,
            rows=rows,
            row_key="label",
            pagination={"rowsPerPage": 0},
        ).classes("w-full").props("flat bordered dense")

        GREEN = COLORS["green"]
        ERROR = COLORS["error"]
        INK2 = COLORS["ink2"]

        table.add_slot("body", r'''
            <q-tr :props="props"
                   :style="props.row._is_total
                     ? 'background: #F0FDF4; font-weight: 700; border-top: 2px solid ''' + GREEN + r''';'
                     : props.row._is_future
                       ? 'opacity: 0.4'
                       : ''">
                <q-td v-for="col in props.cols" :key="col.name" :props="props"
                      :style="'text-align: ' + col.align">
                    <template v-if="col.name === 'pct'">
                        <span :style="{
                            color: props.row._pct_raw > 0
                                ? '''' + GREEN + r''''
                                : props.row._pct_raw < 0
                                    ? '''' + ERROR + r''''
                                    : '''' + INK2 + r'''',
                            fontWeight: props.row._is_total ? 700 : 600
                        }">
                            {{ props.row[col.field] }}
                        </span>
                    </template>
                    <template v-else>
                        {{ props.row[col.field] }}
                    </template>
                </q-td>
            </q-tr>
        ''')
