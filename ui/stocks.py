"""
ui/stocks.py
============
Page Stocks — Analyse de l'autonomie des stocks contenants, groupés par fournisseur.
"""
from __future__ import annotations

import asyncio
import logging

from nicegui import ui

_log = logging.getLogger("ferment.stocks")

from common.easybeer import is_configured as eb_configured
from ui._stocks_calc import StockGroup, StockItem, fetch_and_compute
from ui.auth import require_auth
from ui.theme import COLORS, kpi_card, page_layout, section_title

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _format_days(days: float | None) -> str:
    if days is None:
        return "N/A"
    if days > 365:
        return "> 1 an"
    return f"{days:.0f} j"


def _days_color(days: float | None) -> str:
    if days is None:
        return COLORS["ink2"]
    if days < 14:
        return COLORS["error"]
    if days < 30:
        return COLORS["warning"]
    return COLORS["success"]


def _format_number(n: float, unit: str = "") -> str:
    s = f"{n:,.0f}".replace(",", "\u202f")  # espace fine insécable
    return f"{s} {unit}".strip() if unit else s


def _group_summary(group: StockGroup) -> str:
    """Short summary for expansion panel header badge."""
    n = len(group.items)
    days_list = [it.stock_days for it in group.items if it.stock_days is not None]
    if days_list:
        min_days = min(days_list)
        return f"{n} item{'s' if n > 1 else ''} — min {_format_days(min_days)}"
    return f"{n} item{'s' if n > 1 else ''}"


# ─── Page ─────────────────────────────────────────────────────────────────────


@ui.page("/stocks")
def page_stocks():
    user = require_auth()
    if not user:
        return

    with page_layout("Stocks", "inventory_2", "/stocks") as sidebar:

        with sidebar:
            ui.label("Stocks contenants").classes("text-subtitle2 text-grey-7")
            ui.label("Analyse d'autonomie").classes("text-caption text-grey-5")

        # ── Explication ──────────────────────────────────────────────
        with ui.card().classes("w-full").props("flat bordered"):
            with ui.card_section().classes("q-pa-md"):
                ui.label(
                    "Analysez l'autonomie de vos stocks de contenants. "
                    "Choisissez une période pour calculer la consommation "
                    "moyenne et estimer le nombre de jours de stock restant. "
                    "Les contenants sont regroupés par fournisseur."
                ).classes("text-body2").style(
                    f"color: {COLORS['ink2']}; line-height: 1.6"
                )

        # ── Carte d'analyse ──────────────────────────────────────────
        with ui.card().classes("").props("flat bordered").style(
            "width: 50%; min-width: 320px"
        ):
            with ui.card_section():
                with ui.row().classes("items-center gap-2"):
                    ui.icon("inventory_2", size="sm").style(
                        f"color: {COLORS['green']}"
                    )
                    ui.label("Analyse EasyBeer").classes("text-h6")

            with ui.card_section():
                if not eb_configured():
                    ui.label("EasyBeer non configuré.").classes("text-grey-6")
                else:
                    ui.label("Période d'analyse").classes("text-caption").style(
                        f"color: {COLORS['ink2']}; font-weight: 500"
                    )
                    period_radio = ui.radio(
                        {30: "1 mois", 60: "2 mois", 90: "3 mois", 180: "6 mois"},
                        value=30,
                    ).props("inline dense color=green-8")

                    status_label = ui.label("").classes("text-body2 q-mt-sm")
                    status_label.set_visibility(False)

                    fetch_spinner = ui.spinner(
                        "dots", size="xl", color="green-8",
                    ).classes("self-center q-pa-md")
                    fetch_spinner.set_visibility(False)

                    # Conteneur des résultats (vidé/rerempli à chaque analyse)
                    results_container = ui.column().classes("w-full gap-4 q-mt-md")

                    async def do_fetch():
                        fetch_btn.disable()
                        fetch_spinner.set_visibility(True)
                        status_label.set_visibility(False)
                        results_container.clear()
                        try:
                            days = int(period_radio.value or 30)
                            groups: list[StockGroup] = await asyncio.wait_for(
                                asyncio.to_thread(fetch_and_compute, days),
                                timeout=60,
                            )
                            total_items = sum(len(g.items) for g in groups)
                            if not groups or total_items == 0:
                                status_label.text = (
                                    "Aucun contenant trouvé dans EasyBeer. "
                                    "Vérifiez la configuration des stocks."
                                )
                                status_label.classes("text-negative", remove="text-positive")
                                status_label.set_visibility(True)
                                return
                            _render_groups(results_container, groups, days)
                            status_label.text = (
                                f"Analyse terminée — {total_items} contenant(s) "
                                f"en {len(groups)} groupe(s) sur {days} jours"
                            )
                            status_label.classes("text-positive", remove="text-negative")
                            status_label.set_visibility(True)
                            ui.notify("Analyse terminée !", type="positive")
                        except TimeoutError:
                            status_label.text = (
                                "L'analyse a dépassé le délai (60 s). Réessayez."
                            )
                            status_label.classes("text-negative", remove="text-positive")
                            status_label.set_visibility(True)
                            ui.notify("Délai dépassé", type="warning")
                        except Exception:
                            _log.exception("Erreur analyse stocks contenants")
                            status_label.text = (
                                "Erreur lors de l'analyse. "
                                "Vérifiez la connexion EasyBeer."
                            )
                            status_label.classes("text-negative", remove="text-positive")
                            status_label.set_visibility(True)
                        finally:
                            fetch_spinner.set_visibility(False)
                            fetch_btn.enable()

                    fetch_btn = ui.button(
                        "Analyser les stocks",
                        icon="analytics",
                        on_click=do_fetch,
                    ).classes("w-full q-mt-md").props("color=green-8 unelevated")


# ─── Rendu des résultats ──────────────────────────────────────────────────────


def _render_groups(
    container: ui.column,
    groups: list[StockGroup],
    window_days: int,
) -> None:
    """Render all stock groups as expansion panels."""
    with container:
        for group in groups:
            _render_group_panel(group, window_days)


def _render_group_panel(group: StockGroup, window_days: int) -> None:
    """Render a single supplier group as an expansion panel."""
    summary = _group_summary(group)

    with ui.expansion(value=True).classes("w-full").props(
        "dense header-class=bg-grey-2"
    ) as expansion:
        # Custom header with icon + name + badge
        with expansion.add_slot("header"):
            with ui.row().classes("items-center gap-2 w-full"):
                ui.icon(group.icon, size="sm").style(
                    f"color: {COLORS['green']}"
                )
                ui.label(group.name).classes("text-subtitle1")
                ui.space()
                ui.badge(summary).props("color=grey-6 outline")

        # ── KPI cards ────────────────────────────────────────────
        section_title("Autonomie", "timer")
        with ui.row().classes("w-full gap-4 flex-wrap"):
            for item in group.items:
                kpi_card(
                    icon="inventory_2",
                    label=item.label,
                    value=_format_days(item.stock_days),
                    color=_days_color(item.stock_days),
                )

        # ── Tableau détail ───────────────────────────────────────
        section_title("Détail", "table_chart")
        columns = [
            {"name": "label", "label": "Contenant", "field": "label", "align": "left"},
            {"name": "stock", "label": "Stock actuel", "field": "stock", "align": "right"},
            {"name": "seuil", "label": "Seuil bas", "field": "seuil", "align": "right"},
            {"name": "conso", "label": f"Conso ({window_days} j)", "field": "conso", "align": "right"},
            {"name": "daily", "label": "Conso / jour", "field": "daily", "align": "right"},
            {"name": "days", "label": "Autonomie", "field": "days", "align": "right"},
        ]
        rows = []
        for item in group.items:
            rows.append({
                "label": item.label,
                "stock": _format_number(item.current_stock, item.unit),
                "seuil": _format_number(item.seuil_bas, item.unit) if item.seuil_bas else "—",
                "conso": _format_number(item.consumption, item.unit),
                "daily": f"{item.daily_consumption:,.1f} {item.unit}/j",
                "days": _format_days(item.stock_days),
            })
        ui.table(
            columns=columns,
            rows=rows,
            row_key="label",
        ).classes("w-full").props("flat bordered dense")
