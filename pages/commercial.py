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


# ─── Helpers dashboard (sticky header, KPIs, courbes, heatmap) ──────────────

def _heat_color(pct: float, opacity: float = 1.0) -> str:
    """Couleur HSLA pour le heatmap mensuel : rouge (-30%) → jaune (0) → vert (+30%)."""
    pct = max(-30.0, min(30.0, pct))
    if pct >= 0:
        ratio = pct / 30.0
        h = 45 + (140 - 45) * ratio
        s = 80 - (80 - 60) * ratio
        l = 55 - (55 - 45) * ratio
    else:
        ratio = (-pct) / 30.0
        h = 45 - 45 * ratio
        s = 80 - (80 - 70) * ratio
        l = 55
    return f"hsla({h:.0f}, {s:.0f}%, {l:.0f}%, {opacity})"


def _render_sticky_summary(
    year_a: int, year_b: int,
    ytd_a: float, ytd_b: float, ytd_pct: float,
    ca_cible: float,
) -> None:
    """Bandeau sticky en haut : 4 chiffres clés + barre de progression annuelle."""
    pct_col = _pct_color(ytd_pct)
    cible_progress = (ytd_b / ca_cible * 100) if ca_cible > 0 else 0.0

    with ui.element("div").style(
        "position: sticky; top: 0; z-index: 100; "
        f"background: #FFFFFF; border-bottom: 1px solid {COLORS['border']}; "
        "padding: 10px 16px; margin: -16px -16px 16px -16px; "
        "box-shadow: 0 2px 4px rgba(0,0,0,0.04);"
    ):
        with ui.row().classes("w-full items-center gap-4 no-wrap"):
            with ui.column().classes("gap-0").style("min-width: 130px"):
                ui.label(f"CA {year_b} à date").classes("text-caption").style(
                    f"color: {COLORS['ink2']}; font-weight: 500"
                )
                ui.label(_fmt_eur(ytd_b)).classes("text-h6").style(
                    f"color: {COLORS['ink']}; font-weight: 700; line-height: 1.2"
                )

            ui.separator().props("vertical").style("height: 36px")

            with ui.column().classes("gap-0").style("min-width: 100px"):
                ui.label(f"vs {year_a}").classes("text-caption").style(
                    f"color: {COLORS['ink2']}; font-weight: 500"
                )
                ui.label(_fmt_pct(ytd_pct)).classes("text-h6").style(
                    f"color: {pct_col}; font-weight: 700; line-height: 1.2"
                )

            ui.separator().props("vertical").style("height: 36px")

            with ui.column().classes("gap-0").style("min-width: 130px"):
                ui.label(f"Cible {year_b}").classes("text-caption").style(
                    f"color: {COLORS['ink2']}; font-weight: 500"
                )
                ui.label(_fmt_eur(ca_cible)).classes("text-h6").style(
                    f"color: {COLORS['orange']}; font-weight: 700; line-height: 1.2"
                )

            ui.separator().props("vertical").style("height: 36px")

            with ui.column().classes("flex-1 gap-1"):
                with ui.row().classes("items-center justify-between"):
                    ui.label("Progression cible annuelle").classes("text-caption").style(
                        f"color: {COLORS['ink2']}; font-weight: 500"
                    )
                    ui.label(f"{cible_progress:.1f} %").classes("text-body2").style(
                        f"color: {COLORS['ink']}; font-weight: 600"
                    )
                with ui.element("div").style(
                    "background: #E5E7EB; border-radius: 4px; height: 6px; overflow: hidden"
                ):
                    ui.element("div").style(
                        f"width: {min(cible_progress, 100)}%; height: 100%; "
                        f"background: {COLORS['green']}; border-radius: 4px;"
                        "transition: width 0.5s ease;"
                    )


def _kpi_mini(title: str, value: str, subtitle: str, color: str, icon: str) -> None:
    """Carte KPI compacte (utilisée dans la rangée 'mini-KPIs contextuels')."""
    with ui.card().classes("flex-1 q-pa-none").props("flat bordered"):
        with ui.card_section().classes("q-pa-md"):
            with ui.row().classes("items-center gap-2 q-mb-xs"):
                ui.icon(icon, size="xs").style(f"color: {color}")
                ui.label(title).classes("text-caption").style(
                    f"color: {COLORS['ink2']}; font-weight: 500"
                )
            ui.label(value).classes("text-h6").style(
                f"color: {color}; font-weight: 700"
            )
            ui.label(subtitle).classes("text-caption").style(f"color: {COLORS['ink2']}")


def _render_contextual_kpis(
    months: list[dict[str, Any]],
    ytd_b: float, ca_cible: float,
    year_a: int, year_b: int,
    current_month: int, current_day: int,
) -> None:
    """4 mini-KPIs contextuels : ce mois, M/M-1, rythme, à faire."""
    import datetime as _dt

    cur_m = months[current_month - 1]
    cur_total = cur_m["ca_b_realized"] + cur_m["forecast"]
    cur_pct = cur_m["pct"]

    if current_month >= 2:
        prev_m = months[current_month - 2]
        prev_total = prev_m["ca_b"]
        mm_pct = ((cur_total - prev_total) / prev_total * 100) if prev_total > 0 else 0.0
    else:
        prev_total = 0.0
        mm_pct = 0.0

    today = _dt.date(year_b, current_month, current_day)
    day_of_year = today.timetuple().tm_yday
    daily_rate = ytd_b / day_of_year if day_of_year > 0 else 0.0

    days_in_year = 366 if (year_b % 4 == 0 and year_b % 100 != 0) or year_b % 400 == 0 else 365
    days_remaining = max(1, days_in_year - day_of_year)
    daily_required = max(0.0, (ca_cible - ytd_b) / days_remaining)

    with ui.row().classes("w-full gap-3 q-mb-md"):
        _kpi_mini(
            f"Ce mois ({_MOIS_SHORT[current_month]} en cours)",
            _fmt_eur(cur_total),
            f"{_fmt_pct(cur_pct)} vs {year_a}",
            _pct_color(cur_pct),
            "calendar_today",
        )
        _kpi_mini(
            "vs Mois précédent",
            _fmt_pct(mm_pct),
            f"M-1 = {_fmt_eur(prev_total)}",
            _pct_color(mm_pct),
            "trending_up" if mm_pct >= 0 else "trending_down",
        )
        _kpi_mini(
            "Rythme journalier",
            f"{daily_rate:,.0f} €/j".replace(",", " "),
            f"sur {day_of_year} jours écoulés",
            COLORS["blue"],
            "schedule",
        )
        _kpi_mini(
            "Pour atteindre la cible",
            f"{daily_required:,.0f} €/j".replace(",", " "),
            f"sur {days_remaining} jours restants",
            COLORS["orange"],
            "flag",
        )


def _render_cumulative_chart(
    months: list[dict[str, Any]],
    year_a: int, year_b: int, current_month: int,
) -> None:
    """Courbe cumulative YTD : 2025 (référence) vs 2026 (jusqu'à aujourd'hui)."""
    labels = [m["label"][:3] + "." for m in months]

    cum_a: list[int] = []
    s_a = 0.0
    for m in months:
        s_a += m["ca_a"]
        cum_a.append(round(s_a))

    cum_b: list[Any] = []
    s_b = 0.0
    for m in months:
        if m["month"] <= current_month:
            s_b += m["ca_b_realized"]
            cum_b.append(round(s_b))
        else:
            cum_b.append(None)

    ui.echart({
        "tooltip": {
            "trigger": "axis",
            "valueFormatter": "function (v) { return v == null ? '—' : v.toLocaleString('fr-FR') + ' €'; }",
        },
        "legend": {
            "data": [f"Cumul {year_a}", f"Cumul {year_b}"],
            "top": 5,
        },
        "grid": {"left": 80, "right": 30, "top": 45, "bottom": 35},
        "xAxis": {"type": "category", "data": labels, "boundaryGap": False},
        "yAxis": {"type": "value", "axisLabel": {"formatter": "{value} €"}},
        "series": [
            {
                "name": f"Cumul {year_a}",
                "type": "line",
                "data": cum_a,
                "itemStyle": {"color": "#9CA3AF"},
                "lineStyle": {"width": 2, "type": "dashed"},
                "symbol": "circle", "symbolSize": 6,
            },
            {
                "name": f"Cumul {year_b}",
                "type": "line",
                "data": cum_b,
                "itemStyle": {"color": COLORS["green"]},
                "lineStyle": {"width": 3},
                "symbol": "circle", "symbolSize": 7,
                "areaStyle": {"opacity": 0.15, "color": COLORS["green"]},
            },
        ],
    }).classes("w-full").style("height: 280px")


def _render_monthly_heatmap(
    months: list[dict[str, Any]],
    year_a: int, year_b: int, current_month: int,
) -> None:
    """Bande 12 cellules colorées par évolution % vs N-1. Hover → tooltip détails."""
    with ui.row().classes("w-full no-wrap gap-1"):
        for m in months:
            pct = m["pct"]
            is_future = m["month"] > current_month
            is_current = m["month"] == current_month
            has_data = m["ca_a"] > 0 or m["ca_b"] > 0 or m["forecast"] > 0
            total_b = m["ca_b_realized"] + m["forecast"]

            if not has_data:
                bg = "#F3F4F6"
                txt_col = "#9CA3AF"
            elif is_future:
                bg = _heat_color(pct, opacity=0.45)
                txt_col = "#1F2937"
            else:
                bg = _heat_color(pct, opacity=1.0)
                txt_col = "#FFFFFF"

            border = (
                f"2px solid {COLORS['orange']}" if is_current
                else "1px solid transparent"
            )

            tip_parts = [
                f"{m['label']} {year_b}",
                f"{year_a} : {_fmt_eur(m['ca_a'])}",
                f"{year_b} : {_fmt_eur(m['ca_b'])}" if not is_future else f"{year_b} : —",
            ]
            if m["forecast"] > 0:
                tip_parts.append(f"Prévision : {_fmt_eur(m['forecast'])}")
                tip_parts.append(f"Total prévu : {_fmt_eur(total_b)}")
            tip_parts.append(f"Évolution : {_fmt_pct(pct)}")
            tip = "\n".join(tip_parts)

            with ui.element("div").style(
                f"flex: 1 1 0; min-width: 60px; background: {bg}; color: {txt_col}; "
                f"border: {border}; border-radius: 8px; "
                "padding: 14px 6px; text-align: center; cursor: help; "
                "transition: transform 0.15s ease;"
            ).tooltip(tip):
                ui.label(m["label"][:3]).classes("text-caption").style(
                    "font-weight: 500; opacity: 0.85"
                )
                ui.label(_fmt_pct(pct) if has_data else "—").classes("text-body2").style(
                    "font-weight: 700"
                )


_MOIS_SHORT = [
    "", "Jan", "Fév", "Mar", "Avr", "Mai", "Juin",
    "Juil", "Août", "Sept", "Oct", "Nov", "Déc",
]


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
        # Bandeau sticky + vue d'ensemble synthétique
        # ══════════════════════════════════════════════════════════════
        _render_sticky_summary(
            year_a, year_b, ytd_a, ytd_b, ytd_pct, ca_cible,
        )

        section_title(
            f"Vue d'ensemble — au {current_day:02d}/{current_month:02d}/{year_b}",
            "insights",
        )
        _render_contextual_kpis(
            months, ytd_b, ca_cible,
            year_a, year_b, current_month, current_day,
        )

        # ── Cumulé YTD : visuellement « où on en est »
        section_title(f"Cumul YTD {year_a} vs {year_b}", "show_chart")
        _render_cumulative_chart(months, year_a, year_b, current_month)

        # ── Heatmap mensuelle : lecture instantanée des mois forts/faibles
        section_title("Performance mensuelle", "grid_view")
        _render_monthly_heatmap(months, year_a, year_b, current_month)
        ui.label(
            "Survolez un mois pour voir les détails. "
            f"Cible annuelle calculée sur +{growth_rate:.1f} % YTD."
        ).classes("text-caption q-mt-xs").style(f"color: {COLORS['ink2']}")

        # ── Histogramme + tableau détaillé : repliés par défaut
        with ui.expansion(
            "Détail mensuel — histogramme & tableau",
            icon="table_chart",
            value=False,
        ).classes("w-full q-mt-md").props("header-class=text-subtitle1"):
            _render_chart(months, year_a, year_b, current_month)
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
