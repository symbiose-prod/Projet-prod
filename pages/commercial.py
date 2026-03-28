"""
pages/commercial.py
===================
Dashboard commercial — Comparatif CA mensuel avec prévisions.
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
    """Couleur du pourcentage."""
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
        ca_cible = result["ca_cible"]
        growth_rate = result["growth_rate"]
        current_month = result["current_month"]
        current_day = result["current_day"]

        # ── Section 1 : KPIs Cumul à date ──
        section_title(
            f"Cumul au {current_day:02d}/{current_month:02d}/{year_b}",
            "trending_up",
        )

        with ui.row().classes("w-full gap-4 q-mb-md"):
            # CA YTD année A
            with ui.card().classes("flex-1 q-pa-none").props("flat"):
                with ui.card_section().classes("row items-center gap-3 q-pa-md"):
                    with ui.element("div").classes("q-pa-xs").style(
                        f"background: {COLORS['ink2']}15; border-radius: 6px"
                    ):
                        ui.icon("calendar_month", size="sm").style(f"color: {COLORS['ink2']}")
                    with ui.column().classes("gap-0"):
                        ui.label(f"CA {year_a} (à date)").classes("text-caption").style(
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
                        ui.label(f"CA {year_b} (à date)").classes("text-caption").style(
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
                        ui.label("Évolution").classes("text-caption").style(
                            f"color: {COLORS['ink2']}; font-weight: 500"
                        )
                        ui.label(_fmt_pct(ytd_pct)).classes("text-h6").style(
                            f"color: {pct_col}; font-weight: 600"
                        )

            # CA cible fin d'année
            with ui.card().classes("flex-1 q-pa-none").props("flat"):
                with ui.card_section().classes("row items-center gap-3 q-pa-md"):
                    with ui.element("div").classes("q-pa-xs").style(
                        f"background: {COLORS['orange']}15; border-radius: 6px"
                    ):
                        ui.icon("flag", size="sm").style(f"color: {COLORS['orange']}")
                    with ui.column().classes("gap-0"):
                        ui.label(f"CA cible {year_b}").classes("text-caption").style(
                            f"color: {COLORS['ink2']}; font-weight: 500"
                        )
                        ui.label(_fmt_eur(ca_cible)).classes("text-h6").style(
                            f"color: {COLORS['ink']}; font-weight: 600"
                        )
                        ui.label(
                            "Basé sur {}% (2 derniers mois)".format(
                                f"+{growth_rate:.1f}" if growth_rate > 0 else f"{growth_rate:.1f}"
                            )
                        ).classes("text-caption").style(f"color: {COLORS['ink2']}")

        # ── Section 2 : Histogramme mensuel ──
        section_title(f"CA mensuel {year_a} vs {year_b}", "bar_chart")

        mois_labels = [m["label"][:3] + "." for m in months]

        # Barres CA année A (gris)
        ca_a_vals = [round(m["ca_a"]) for m in months]

        # Barres CA année B réalisé (vert) — avec label % sur la barre du haut
        ca_b_realized = []
        for m in months:
            ca_b_realized.append(round(m["ca_b_realized"]))

        # Barres prévision (orange) — avec label % au sommet de la pile
        ca_forecast = []
        for m in months:
            val = round(m["forecast"])
            has_data = m["ca_a"] > 0 or m["ca_b"] > 0 or m["forecast"] > 0
            pct = m["pct"]
            pct_str = f"{'+'if pct > 0 else ''}{pct:.0f}%" if has_data else ""
            # Le label est sur la prévision si elle existe, sinon sur le réalisé
            if val > 0:
                ca_forecast.append({"value": val, "label": {"show": True, "formatter": pct_str}})
            else:
                ca_forecast.append({"value": val, "label": {"show": False}})
                # Mettre le label sur le réalisé
                if pct_str and ca_b_realized:
                    idx = m["month"] - 1
                    ca_b_realized[idx] = {
                        "value": ca_b_realized[idx] if isinstance(ca_b_realized[idx], int) else ca_b_realized[idx],
                        "label": {"show": True, "formatter": pct_str},
                    }

        GREEN = COLORS["green"]
        ORANGE = COLORS["orange"]

        ui.echart({
            "tooltip": {
                "trigger": "axis",
                "axisPointer": {"type": "shadow"},
            },
            "legend": {
                "data": [str(year_a), f"{year_b} réalisé", f"{year_b} prévision"],
                "top": 10,
            },
            "grid": {
                "left": 80,
                "right": 30,
                "top": 60,
                "bottom": 40,
            },
            "xAxis": {
                "type": "category",
                "data": mois_labels,
            },
            "yAxis": {
                "type": "value",
                "axisLabel": {"formatter": "{value} €"},
            },
            "series": [
                {
                    "name": str(year_a),
                    "type": "bar",
                    "data": ca_a_vals,
                    "itemStyle": {"color": "#D1D5DB"},
                    "barGap": "5%",
                },
                {
                    "name": f"{year_b} réalisé",
                    "type": "bar",
                    "stack": f"ca_{year_b}",
                    "data": ca_b_realized,
                    "itemStyle": {"color": GREEN},
                    "label": {
                        "show": False,
                        "position": "top",
                        "fontSize": 11,
                        "fontWeight": "bold",
                        "color": "#374151",
                    },
                },
                {
                    "name": f"{year_b} prévision",
                    "stack": f"ca_{year_b}",
                    "type": "bar",
                    "data": ca_forecast,
                    "itemStyle": {
                        "color": ORANGE,
                        "opacity": 0.5,
                    },
                    "label": {
                        "show": False,
                        "position": "top",
                        "fontSize": 11,
                        "fontWeight": "bold",
                        "color": "#374151",
                    },
                },
            ],
        }).classes("w-full").style("height: 420px")

        # ── Tableau détaillé ──
        section_title(f"Détail mensuel {year_a} vs {year_b}", "table_chart")

        columns = [
            {"name": "label", "label": "Mois", "field": "label", "align": "left"},
            {"name": "ca_a", "label": f"CA {year_a}", "field": "ca_a", "align": "right"},
            {"name": "ca_b", "label": f"CA {year_b}", "field": "ca_b", "align": "right"},
            {"name": "forecast", "label": "Prévision", "field": "forecast", "align": "right"},
            {"name": "pct", "label": "Évolution", "field": "pct", "align": "right"},
        ]

        rows = []
        for m in months:
            is_future = m["month"] > current_month
            is_current = m["month"] == current_month
            total_b = m["ca_b_realized"] + m["forecast"]

            rows.append({
                "label": m["label"],
                "ca_a": _fmt_eur(m["ca_a"]),
                "ca_b": _fmt_eur(m["ca_b"]) if not is_future else "—",
                "forecast": _fmt_eur(m["forecast"]) if m["forecast"] > 0 else "—",
                "pct": _fmt_pct(m["pct"]) if (m["ca_a"] > 0 or total_b > 0) else "—",
                "_pct_raw": m["pct"],
                "_is_future": is_future,
                "_is_current": is_current,
            })

        # Ligne totale
        total_a = sum(m["ca_a"] for m in months)
        total_b_real = sum(m["ca_b_realized"] for m in months)
        total_forecast = sum(m["forecast"] for m in months)
        total_pct = round((ca_cible - total_a) / total_a * 100, 1) if total_a > 0 else 0.0

        rows.append({
            "label": f"TOTAL {year_b} (réalisé + prévision)",
            "ca_a": _fmt_eur(total_a),
            "ca_b": _fmt_eur(total_b_real),
            "forecast": _fmt_eur(total_forecast),
            "pct": _fmt_pct(total_pct),
            "_pct_raw": total_pct,
            "_is_future": False,
            "_is_current": False,
            "_is_total": True,
        })

        table = ui.table(
            columns=columns,
            rows=rows,
            row_key="label",
            pagination={"rowsPerPage": 0},
        ).classes("w-full").props("flat bordered dense")

        _GREEN = COLORS["green"]
        _ERROR = COLORS["error"]
        _INK2 = COLORS["ink2"]
        _ORANGE = COLORS["orange"]

        table.add_slot("body", r'''
            <q-tr :props="props"
                   :style="props.row._is_total
                     ? 'background: #F0FDF4; font-weight: 700; border-top: 2px solid ''' + _GREEN + r''';'
                     : props.row._is_current
                       ? 'background: #FFFBEB;'
                       : props.row._is_future
                         ? 'opacity: 0.5; font-style: italic;'
                         : ''">
                <q-td v-for="col in props.cols" :key="col.name" :props="props"
                      :style="'text-align: ' + col.align">
                    <template v-if="col.name === 'pct'">
                        <span :style="{
                            color: props.row._pct_raw > 0
                                ? '''' + _GREEN + r''''
                                : props.row._pct_raw < 0
                                    ? '''' + _ERROR + r''''
                                    : '''' + _INK2 + r'''',
                            fontWeight: 600,
                        }">
                            {{ props.row[col.field] }}
                        </span>
                    </template>
                    <template v-else-if="col.name === 'forecast'">
                        <span :style="{color: '''' + _ORANGE + r'''', fontWeight: 500}">
                            {{ props.row[col.field] }}
                        </span>
                    </template>
                    <template v-else>
                        {{ props.row[col.field] }}
                    </template>
                </q-td>
            </q-tr>
        ''')
