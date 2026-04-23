"""
pages/commercial.py
===================
Dashboard commercial — Comparatif CA mensuel avec prévisions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from nicegui import ui

from pages.auth import require_auth
from pages.theme import COLORS, page_layout, section_title

_log = logging.getLogger("ferment.commercial")


def _fmt_eur(v: float) -> str:
    if v == 0:
        return "—"
    return f"{v:,.0f} €".replace(",", " ")


def _fmt_pct(v: float) -> str:
    if v == 0:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f} %"


def _pct_color(v: float) -> str:
    if v > 0:
        return COLORS["green"]
    if v < 0:
        return COLORS["error"]
    return COLORS["ink2"]


# ─── Graphique réutilisable ──────────────────────────────────────────────────

def _render_chart(
    months: list[dict[str, Any]],
    year_a: int,
    year_b: int,
    current_month: int,
) -> None:
    """Rend un histogramme ECharts CA mensuel (réalisé + prévision)."""
    GREEN = COLORS["green"]
    ORANGE = COLORS["orange"]

    mois_labels = [m["label"][:3] + "." for m in months]
    ca_a_vals = [round(m["ca_a"]) for m in months]

    ca_b_realized: list[Any] = []
    ca_forecast: list[Any] = []

    for m in months:
        ca_b_realized.append(round(m["ca_b_realized"]))
        val = round(m["forecast"])
        has_data = m["ca_a"] > 0 or m["ca_b"] > 0 or m["forecast"] > 0
        pct = m["pct"]
        pct_str = ("+" if pct > 0 else "") + f"{pct:.0f}%" if has_data else ""

        if val > 0:
            ca_forecast.append({"value": val, "label": {"show": True, "formatter": pct_str}})
        else:
            ca_forecast.append({"value": val, "label": {"show": False}})
            if pct_str:
                idx = m["month"] - 1
                raw = ca_b_realized[idx]
                ca_b_realized[idx] = {
                    "value": raw if isinstance(raw, int) else raw,
                    "label": {"show": True, "formatter": pct_str},
                }

    ui.echart({
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {
            "data": [str(year_a), f"{year_b} réalisé", f"{year_b} prévision"],
            "top": 10,
        },
        "grid": {"left": 80, "right": 30, "top": 60, "bottom": 40},
        "xAxis": {"type": "category", "data": mois_labels},
        "yAxis": {"type": "value", "axisLabel": {"formatter": "{value} €"}},
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
                "label": {"show": False, "position": "top", "fontSize": 11,
                          "fontWeight": "bold", "color": "#374151"},
            },
            {
                "name": f"{year_b} prévision",
                "stack": f"ca_{year_b}",
                "type": "bar",
                "data": ca_forecast,
                "itemStyle": {"color": ORANGE, "opacity": 0.5},
                "label": {"show": False, "position": "top", "fontSize": 11,
                          "fontWeight": "bold", "color": "#374151"},
            },
        ],
    }).classes("w-full").style("height: 420px")


# ─── Page ────────────────────────────────────────────────────────────────────

@ui.page("/commercial")
async def page_commercial():
    user = require_auth()
    if not user:
        return

    with page_layout("Dashboard Commercial", "bar_chart", "/commercial"):

        # ── Chargement ──
        with ui.column().classes("w-full items-center q-pa-xl"):
            spinner = ui.spinner("dots", size="lg", color="green")
            status_label = ui.label("Chargement des données CA...").classes(
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

        # ══════════════════════════════════════════════════════════════
        # Section 1 : KPIs Cumul à date
        # ══════════════════════════════════════════════════════════════
        section_title(
            f"Cumul au {current_day:02d}/{current_month:02d}/{year_b}",
            "trending_up",
        )

        with ui.row().classes("w-full gap-4 q-mb-md"):
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
                            "Basé sur {}% (évolution YTD)".format(
                                f"+{growth_rate:.1f}" if growth_rate > 0 else f"{growth_rate:.1f}"
                            )
                        ).classes("text-caption").style(f"color: {COLORS['ink2']}")

        # ══════════════════════════════════════════════════════════════
        # Section 2 : Histogramme global
        # ══════════════════════════════════════════════════════════════
        section_title(f"CA mensuel {year_a} vs {year_b}", "bar_chart")
        _render_chart(months, year_a, year_b, current_month)

        # ── Tableau détaillé ──
        _render_table(months, year_a, year_b, current_month, ca_cible)

        # ══════════════════════════════════════════════════════════════
        # Section 3 : CA par tag (filtrable)
        # ══════════════════════════════════════════════════════════════
        section_title("CA par tag", "sell")

        # Charger les tags disponibles
        from common._session import current_tenant_id
        from common.client_cache import get_all_tags

        tid = current_tenant_id()
        all_tags = get_all_tags(tid)
        tag_options = [t["tag"] for t in all_tags]

        if not tag_options:
            ui.label(
                "Aucun tag disponible. Lance la synchronisation depuis Paramètres → Tags clients."
            ).classes("text-grey-6 q-pa-md")
        else:
            with ui.row().classes("w-full items-end gap-3"):
                tag_select = ui.select(
                    tag_options,
                    label="Sélectionner un tag",
                    value=None,
                ).classes("flex-1").props("outlined dense clearable")

                tag_btn = ui.button(
                    "Charger", icon="search", on_click=lambda: None,
                ).props("color=green-8 unelevated")

            tag_chart_container = ui.column().classes("w-full")
            tag_status = ui.label("").classes("text-caption text-grey-6")

            async def _load_tag_ca():
                tag = tag_select.value
                if not tag:
                    ui.notify("Sélectionne un tag.", type="warning")
                    return

                tag_chart_container.clear()
                tag_status.text = f"Chargement CA pour le tag « {tag} »..."
                tag_btn.disable()

                try:
                    from pages._commercial_calc import fetch_ca_comparison_with_tag

                    tag_result = await asyncio.to_thread(
                        fetch_ca_comparison_with_tag, tag, year_a, year_b,
                    )

                    tag_chart_container.clear()
                    with tag_chart_container:
                        section_title(f"CA « {tag} » — {year_a} vs {year_b}", "sell")
                        _render_chart(
                            tag_result["months"], year_a, year_b, current_month,
                        )
                        _render_table(
                            tag_result["months"], year_a, year_b, current_month,
                            tag_result["ca_cible"],
                        )

                    tag_status.text = (
                        f"Tag « {tag} » : taux glissant "
                        f"{'+' if tag_result['growth_rate'] > 0 else ''}"
                        f"{tag_result['growth_rate']:.1f}%"
                    )
                except Exception as exc:
                    _log.exception("Erreur chargement CA tag %s", tag)
                    tag_status.text = f"Erreur : {exc}"
                    tag_status.classes("text-negative")
                finally:
                    tag_btn.enable()

            tag_btn.on_click(_load_tag_ca)

        # ══════════════════════════════════════════════════════════════
        # Section 4 : Objectifs annuels par marque / enseigne
        # ══════════════════════════════════════════════════════════════
        from common.data import get_commercial_config

        comm_cfg = get_commercial_config()
        obj_cfg = comm_cfg.get("objectives") or {}
        obj_brands = obj_cfg.get("brands") or []

        if obj_brands:
            obj_year = obj_cfg.get("year", 2026)
            obj_year_ref = obj_cfg.get("year_ref", 2025)

            section_title(
                f"Objectifs {obj_year} — suivi par enseigne", "flag",
            )

            # Conteneur pour le chargement asynchrone
            obj_container = ui.column().classes("w-full gap-4")

            with obj_container:
                with ui.row().classes("w-full items-center gap-2 q-pa-md"):
                    obj_spinner = ui.spinner("dots", size="md", color="green")
                    ui.label("Chargement du suivi des objectifs...").classes(
                        "text-caption text-grey-6"
                    )

            async def _load_objectives():
                try:
                    from pages._commercial_calc import fetch_objectives_tracking

                    obj_result = await asyncio.to_thread(
                        fetch_objectives_tracking, obj_cfg,
                    )
                except Exception as exc:
                    _log.exception("Erreur chargement objectifs")
                    obj_container.clear()
                    with obj_container:
                        ui.label(f"Erreur : {exc}").classes("text-negative q-pa-md")
                    return

                obj_container.clear()
                with obj_container:
                    _render_objectives_section(obj_result, obj_year, obj_year_ref)

            # Lancer le chargement des objectifs
            asyncio.ensure_future(_load_objectives())


# ─── Objectifs — rendu UI ──────────────────────────────────────────────────

def _progress_color(pct: float) -> str:
    """Couleur de la jauge selon l'avancement."""
    if pct >= 80:
        return COLORS["green"]
    if pct >= 50:
        return COLORS["orange"]
    return COLORS["error"]


def _render_objectives_section(
    data: dict[str, Any],
    year: int,
    year_ref: int,
) -> None:
    """Rendu complet de la section objectifs : KPIs par marque + graphiques par enseigne."""

    brands = data.get("brands") or []
    current_month = data.get("current_month", 1)

    # ── KPI par marque (Symbiose / Niko) ────────────────────────
    with ui.row().classes("w-full gap-4 q-mb-lg"):
        for brand in brands:
            ca_realized = brand.get("ca_realized", 0)
            ca_ref_total = brand.get("ca_ref_total", 0)
            target = brand.get("target", 0)
            pct = brand.get("progress_pct", 0)
            label = brand.get("label", brand.get("tag", "?"))
            color = _progress_color(pct)
            has_error = brand.get("_error", False)
            target_delta = brand.get("target_delta", 0)

            with ui.card().classes("flex-1 q-pa-none").props("flat"):
                with ui.card_section().classes("q-pa-md"):
                    with ui.row().classes("items-center gap-3 q-mb-sm"):
                        with ui.element("div").classes("q-pa-xs").style(
                            f"background: {color}15; border-radius: 6px"
                        ):
                            ui.icon("flag", size="sm").style(f"color: {color}")
                        ui.label(label).classes("text-subtitle1").style(
                            f"color: {COLORS['ink']}; font-weight: 600"
                        )

                    if has_error:
                        ui.label("Données indisponibles (erreur API EasyBeer)").classes(
                            "text-caption text-negative"
                        )
                    else:
                        # CA réalisé / objectif
                        with ui.row().classes("items-baseline gap-2"):
                            ui.label(_fmt_eur(ca_realized)).classes("text-h5").style(
                                f"color: {COLORS['ink']}; font-weight: 700"
                            )
                            ui.label(f"/ {_fmt_eur(target)}").classes(
                                "text-body2 text-grey-6"
                            )

                        # Barre de progression
                        bar_pct = min(pct, 100)
                        with ui.element("div").classes("w-full q-mt-sm").style(
                            "background: #E5E7EB; border-radius: 4px; height: 8px; overflow: hidden"
                        ):
                            ui.element("div").style(
                                f"width: {bar_pct}%; height: 100%; "
                                f"background: {color}; border-radius: 4px; "
                                f"transition: width 0.5s ease"
                            )

                        # Détails sous la barre
                        with ui.row().classes("w-full justify-between q-mt-sm"):
                            with ui.column().classes("gap-0"):
                                delta_str = f"+{target_delta:,.0f} €".replace(",", " ")
                                ui.label(f"Objectif croissance : {delta_str}").classes(
                                    "text-caption text-grey-6"
                                )
                                ui.label(
                                    f"CA {year_ref} : {_fmt_eur(ca_ref_total)}"
                                ).classes("text-caption text-grey-5")
                            ui.label(f"{pct:.0f} %").classes("text-h6").style(
                                f"color: {color}; font-weight: 700"
                            )

    # ── Graphique récapitulatif par enseigne ────────────────────
    for brand in brands:
        enseignes = brand.get("enseignes") or []
        if not enseignes:
            continue

        section_title(
            f"{brand.get('label', '?')} — CA {year} vs Objectif {year} (à date)",
            "storefront",
        )

        GREEN = COLORS["green"]
        INK = COLORS["ink"]

        # Préparer les données pour le graphique unique
        ens_labels: list[str] = []
        ca_realized_vals: list[int] = []
        obj_ytd_vals: list[int] = []

        for ens in enseignes:
            ens_label = ens.get("label", ens.get("tag", "?"))
            ens_months = ens.get("months") or []
            ca_real = ens.get("ca_realized", 0)
            has_error = ens.get("_error", False)

            # Objectif YTD = somme des objectifs mensuels jusqu'au mois en cours
            obj_ytd = 0.0
            if not has_error:
                for m_data in ens_months:
                    if m_data["month"] <= current_month:
                        obj_ytd += m_data.get("objective", 0)

            ens_labels.append(ens_label)
            ca_realized_vals.append(round(ca_real))
            obj_ytd_vals.append(round(obj_ytd))

        ui.echart({
            "tooltip": {
                "trigger": "axis",
                "axisPointer": {"type": "shadow"},
                "formatter": None,
            },
            "legend": {
                "data": [
                    f"CA {year} (réalisé)",
                    f"Objectif {year} (à date)",
                ],
                "top": 5,
                "textStyle": {"fontSize": 12},
            },
            "grid": {
                "left": 80, "right": 30,
                "top": 45, "bottom": 60,
            },
            "xAxis": {
                "type": "category",
                "data": ens_labels,
                "axisLabel": {
                    "rotate": 20,
                    "fontSize": 11,
                    "fontWeight": "bold",
                },
            },
            "yAxis": {
                "type": "value",
                "axisLabel": {"formatter": "{value} €"},
            },
            "series": [
                {
                    "name": f"CA {year} (réalisé)",
                    "type": "bar",
                    "data": ca_realized_vals,
                    "itemStyle": {"color": GREEN},
                    "barGap": "10%",
                    "label": {
                        "show": True,
                        "position": "top",
                        "fontSize": 10,
                        "formatter": "{c} €",
                    },
                },
                {
                    "name": f"Objectif {year} (à date)",
                    "type": "bar",
                    "data": obj_ytd_vals,
                    "itemStyle": {"color": INK},
                    "label": {
                        "show": True,
                        "position": "top",
                        "fontSize": 10,
                        "formatter": "{c} €",
                    },
                },
            ],
        }).classes("w-full").style("height: 420px")


# ─── Tableau détaillé (réutilisable) ────────────────────────────────────────

def _render_table(
    months: list[dict[str, Any]],
    year_a: int,
    year_b: int,
    current_month: int,
    ca_cible: float,
) -> None:
    """Rend le tableau mensuel avec % colorés."""
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

    _GREEN = COLORS["green"]
    _ERROR = COLORS["error"]
    _INK2 = COLORS["ink2"]
    _ORANGE = COLORS["orange"]

    table = ui.table(
        columns=columns,
        rows=rows,
        row_key="label",
        pagination={"rowsPerPage": 0},
    ).classes("w-full").props("flat bordered dense")

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
