"""
ui/stocks.py
============
Page Stocks — Analyse de l'autonomie des stocks contenants, groupés par fournisseur.
Sélection du fournisseur dans la sidebar, puis analyse par période.
"""
from __future__ import annotations

import asyncio
import logging

from nicegui import ui

_log = logging.getLogger("ferment.stocks")

from common.data import get_stocks_config
from common.easybeer import is_configured as eb_configured
from ui._stocks_calc import (
    OrderRecommendation,
    StockGroup,
    compute_order_recommendation,
    fetch_and_compute,
)
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

    # Charger la config fournisseurs
    stocks_cfg = get_stocks_config()
    supplier_groups = stocks_cfg.get("supplier_groups") or []
    # Tous les noms pour le sélecteur
    supplier_options = [g["name"] for g in supplier_groups]
    # Fournisseurs avec patterns = analyse contenants disponible
    _analysable = {
        g["name"] for g in supplier_groups if g.get("patterns")
    }

    with page_layout("Stocks", "inventory_2", "/stocks") as sidebar:

        # ── Sidebar : liste fournisseurs par catégorie ────────────────
        with sidebar:
            if not eb_configured():
                ui.label("EasyBeer non configuré.").classes(
                    "text-caption text-grey-5"
                )
            elif not supplier_options:
                ui.label("Aucun fournisseur configuré.").classes(
                    "text-caption text-grey-5"
                )
            else:
                # État de sélection
                selected_supplier = {"value": None}
                supplier_buttons: dict[str, ui.button] = {}

                def select_supplier(name: str):
                    prev = selected_supplier["value"]
                    # Dé-sélectionner l'ancien
                    if prev and prev in supplier_buttons:
                        supplier_buttons[prev].props(
                            "color=grey-8", remove="color=green-8"
                        )
                        supplier_buttons[prev].classes(
                            remove="nav-active"
                        )
                    # Sélectionner le nouveau (ou désélectionner si même)
                    if name == prev:
                        selected_supplier["value"] = None
                        _on_supplier_selected(None)
                    else:
                        selected_supplier["value"] = name
                        supplier_buttons[name].props(
                            "color=green-8", remove="color=grey-8"
                        )
                        supplier_buttons[name].classes("nav-active")
                        _on_supplier_selected(name)

                # Grouper par catégorie
                categories: dict[str, list[dict]] = {}
                for g in supplier_groups:
                    cat = g.get("category", "Autres")
                    categories.setdefault(cat, []).append(g)

                for cat_name, items in categories.items():
                    ui.label(cat_name).classes(
                        "text-caption text-grey-6 q-mt-sm"
                    ).style("font-weight: 600; text-transform: uppercase")
                    for g in items:
                        btn = ui.button(
                            g["name"],
                            icon=g.get("icon", "category"),
                            on_click=lambda _, n=g["name"]: select_supplier(n),
                        ).classes(
                            "w-full justify-start q-mb-xs"
                        ).props(
                            "flat align=left color=grey-8"
                        ).style(
                            "font-size: 13px; text-transform: none; "
                            "letter-spacing: 0"
                        )
                        supplier_buttons[g["name"]] = btn

        # ── Explication ──────────────────────────────────────────────
        with ui.card().classes("w-full").props("flat bordered"):
            with ui.card_section().classes("q-pa-md"):
                ui.label(
                    "Analysez l'autonomie de vos stocks de contenants. "
                    "Sélectionnez un fournisseur dans le menu latéral, "
                    "choisissez une période, puis lancez l'analyse."
                ).classes("text-body2").style(
                    f"color: {COLORS['ink2']}; line-height: 1.6"
                )

        if not eb_configured() or not supplier_options:
            return

        # ── Placeholder (aucun fournisseur sélectionné) ─────────────
        placeholder_msg = ui.card().classes("w-full").props("flat bordered")
        with placeholder_msg:
            with ui.card_section().classes("q-pa-lg text-center"):
                ui.icon("touch_app", size="xl").style(
                    f"color: {COLORS['ink2']}; opacity: 0.4"
                )
                ui.label(
                    "Sélectionnez un fournisseur dans le menu "
                    "pour lancer une analyse."
                ).classes("text-body1 q-mt-sm").style(
                    f"color: {COLORS['ink2']}"
                )

        # ── Placeholder "bientôt disponible" (MP/emballages) ────────
        coming_soon_card = ui.card().classes("w-full").props("flat bordered")
        coming_soon_card.set_visibility(False)
        with coming_soon_card:
            with ui.card_section().classes("q-pa-lg text-center"):
                ui.icon("construction", size="xl").style(
                    f"color: {COLORS['warning']}; opacity: 0.6"
                )
                coming_soon_name = ui.label("").classes(
                    "text-h6 q-mt-sm"
                )
                ui.label(
                    "L'analyse des stocks pour ce fournisseur "
                    "sera bientôt disponible."
                ).classes("text-body2 q-mt-xs").style(
                    f"color: {COLORS['ink2']}"
                )

        # ── Bloc d'analyse (masqué par défaut) ──────────────────────
        analysis_card = ui.card().classes("w-full").props("flat bordered")
        analysis_card.set_visibility(False)

        with analysis_card:
            with ui.card_section():
                with ui.row().classes("items-center gap-2"):
                    ui.icon("analytics", size="sm").style(
                        f"color: {COLORS['green']}"
                    )
                    supplier_header = ui.label("Analyse").classes("text-h6")

            with ui.card_section():
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

                # Conteneur des résultats
                results_container = ui.column().classes("w-full gap-4 q-mt-md")

                async def do_fetch():
                    fetch_btn.disable()
                    fetch_spinner.set_visibility(True)
                    status_label.set_visibility(False)
                    results_container.clear()
                    try:
                        days = int(period_radio.value or 30)
                        selected = selected_supplier["value"]
                        groups: list[StockGroup] = await asyncio.wait_for(
                            asyncio.to_thread(fetch_and_compute, days),
                            timeout=60,
                        )
                        # Filtrer pour le fournisseur sélectionné
                        filtered = [
                            g for g in groups if g.name == selected
                        ]
                        total_items = sum(len(g.items) for g in filtered)
                        if not filtered or total_items == 0:
                            status_label.text = (
                                f"Aucun contenant trouvé pour {selected}. "
                                "Vérifiez la configuration des stocks."
                            )
                            status_label.classes(
                                "text-negative", remove="text-positive"
                            )
                            status_label.set_visibility(True)
                            return
                        # Build ordering config map
                        ordering_cfgs = {
                            g["name"]: g.get("ordering", {})
                            for g in supplier_groups
                            if g.get("ordering")
                        }
                        _render_groups(
                            results_container, filtered, days, ordering_cfgs,
                        )
                        status_label.text = (
                            f"Analyse terminée — {total_items} contenant(s) "
                            f"sur {days} jours"
                        )
                        status_label.classes(
                            "text-positive", remove="text-negative"
                        )
                        status_label.set_visibility(True)
                        ui.notify("Analyse terminée !", type="positive")
                    except TimeoutError:
                        status_label.text = (
                            "L'analyse a dépassé le délai (60 s). Réessayez."
                        )
                        status_label.classes(
                            "text-negative", remove="text-positive"
                        )
                        status_label.set_visibility(True)
                        ui.notify("Délai dépassé", type="warning")
                    except Exception:
                        _log.exception("Erreur analyse stocks contenants")
                        status_label.text = (
                            "Erreur lors de l'analyse. "
                            "Vérifiez la connexion EasyBeer."
                        )
                        status_label.classes(
                            "text-negative", remove="text-positive"
                        )
                        status_label.set_visibility(True)
                    finally:
                        fetch_spinner.set_visibility(False)
                        fetch_btn.enable()

                fetch_btn = ui.button(
                    "Analyser les stocks",
                    icon="analytics",
                    on_click=do_fetch,
                ).classes("w-full q-mt-md").props("color=green-8 unelevated")

        # ── Callback sélection fournisseur ──────────────────────────
        def _on_supplier_selected(name: str | None):
            # Masquer tout par défaut
            placeholder_msg.set_visibility(False)
            analysis_card.set_visibility(False)
            coming_soon_card.set_visibility(False)

            if not name:
                placeholder_msg.set_visibility(True)
            elif name in _analysable:
                analysis_card.set_visibility(True)
                supplier_header.text = f"Analyse — {name}"
                results_container.clear()
                status_label.set_visibility(False)
            else:
                coming_soon_card.set_visibility(True)
                coming_soon_name.text = name


# ─── Rendu des résultats ──────────────────────────────────────────────────────


def _render_groups(
    container: ui.column,
    groups: list[StockGroup],
    window_days: int,
    ordering_cfgs: dict[str, dict],
) -> None:
    """Render all stock groups as expansion panels."""
    with container:
        for group in groups:
            ordering = ordering_cfgs.get(group.name, {})
            _render_group_panel(group, window_days, ordering)


def _render_group_panel(
    group: StockGroup, window_days: int, ordering_cfg: dict,
) -> None:
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

        # ── Recommandation de commande ──────────────────────────
        rec = compute_order_recommendation(group, ordering_cfg)
        if rec:
            _render_order_section(rec)


# ─── Section commande ────────────────────────────────────────────────────────


_URGENCY_COLORS = {
    "critical": COLORS["error"],
    "warning": COLORS["warning"],
    "ok": COLORS["success"],
}
_URGENCY_LABELS = {
    "critical": "URGENT",
    "warning": "A planifier",
    "ok": "Stock OK",
}
_URGENCY_ICONS = {
    "critical": "error",
    "warning": "schedule",
    "ok": "check_circle",
}

_MONTHS_FR = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def _format_date_fr(d) -> str:
    """Format a date as 'DD mois YYYY' in French."""
    if d is None:
        return "—"
    return f"{d.day} {_MONTHS_FR[d.month]} {d.year}"


def _render_order_section(rec: OrderRecommendation) -> None:
    """Render the ordering recommendation section."""
    color = _URGENCY_COLORS[rec.urgency]

    section_title("Recommandation de commande", "local_shipping")

    # ── KPI cards: deadline per reference ──────────────────────
    with ui.row().classes("w-full gap-4 flex-wrap"):
        for oi in rec.items:
            if oi.days_before_order is not None and oi.days_before_order <= 0:
                item_color = COLORS["error"]
                val = "En retard"
            elif oi.days_before_order is not None and oi.days_before_order <= 14:
                item_color = COLORS["warning"]
                val = _format_date_fr(oi.deadline)
            elif oi.deadline:
                item_color = COLORS["success"]
                val = _format_date_fr(oi.deadline)
            else:
                item_color = COLORS["ink2"]
                val = "N/A"
            kpi_card(
                icon="event",
                label=oi.label,
                value=val,
                color=item_color,
            )

    # ── Coverage bars + summary card ──────────────────────────
    with ui.card().classes("w-full q-mt-sm").props("flat bordered"):
        with ui.card_section().classes("q-pa-md"):
            # Header with urgency badge
            with ui.row().classes("items-center gap-2 q-mb-md"):
                ui.icon(
                    _URGENCY_ICONS[rec.urgency], size="sm",
                ).style(f"color: {color}")
                ui.label(
                    f"Commande {rec.supplier}",
                ).classes("text-subtitle1").style("font-weight: 600")
                ui.space()
                ui.badge(
                    _URGENCY_LABELS[rec.urgency],
                ).props(f"color={_q_color(rec.urgency)}")

            # Coverage bars per reference
            max_days = max(
                (oi.stock_days or 0 for oi in rec.items), default=60,
            )
            bar_max = max(max_days, rec.lead_time_days * 3, 60)

            for oi in rec.items:
                _render_coverage_bar(oi, rec.lead_time_days, bar_max)

            ui.separator().classes("q-my-md")

            # Order summary
            with ui.row().classes("items-start gap-6 flex-wrap"):
                # Left: key info
                with ui.column().classes("gap-1"):
                    _info_line(
                        "Date limite commande",
                        _format_date_fr(rec.order_deadline),
                        bold=True,
                    )
                    _info_line(
                        "Délai livraison",
                        f"{rec.lead_time_days} jours",
                    )
                    _info_line(
                        "Commande minimum",
                        f"{rec.min_pallets} palettes",
                    )

                # Right: pallet breakdown
                with ui.column().classes("gap-1"):
                    ui.label("Répartition suggérée").classes(
                        "text-caption",
                    ).style(
                        f"color: {COLORS['ink2']}; font-weight: 600"
                    )
                    for oi in rec.items:
                        # Short label: take last meaningful part
                        short = oi.label.split(" - ")[0] if " - " in oi.label else oi.label
                        palettes_txt = (
                            f"{oi.suggested_palettes} pal."
                            f" = {_format_number(oi.suggested_qty)} btl"
                        )
                        coverage_txt = (
                            f"(~{oi.coverage_days:.0f} j)"
                            if oi.coverage_days else ""
                        )
                        with ui.row().classes("items-center gap-2"):
                            ui.icon("inventory_2", size="xs").style(
                                f"color: {COLORS['ink2']}"
                            )
                            ui.label(
                                f"{short} : {palettes_txt} {coverage_txt}",
                            ).classes("text-body2")


def _q_color(urgency: str) -> str:
    """Map urgency to Quasar color name for badge."""
    return {"critical": "red-6", "warning": "amber-8", "ok": "green-7"}[urgency]


def _info_line(label: str, value: str, bold: bool = False) -> None:
    """Render a label: value line."""
    with ui.row().classes("items-center gap-2"):
        ui.label(label).classes("text-caption").style(
            f"color: {COLORS['ink2']}; font-weight: 500"
        )
        weight = "700" if bold else "600"
        ui.label(value).classes("text-body2").style(
            f"color: {COLORS['ink']}; font-weight: {weight}"
        )


def _render_coverage_bar(oi, lead_time_days: int, bar_max: float) -> None:
    """Render a horizontal coverage bar for one reference."""
    stock_days = oi.stock_days or 0
    pct_stock = min(stock_days / bar_max * 100, 100) if bar_max > 0 else 0
    pct_lead = min(lead_time_days / bar_max * 100, 100) if bar_max > 0 else 0

    # Bar color based on stock vs lead time
    if stock_days <= lead_time_days:
        bar_color = COLORS["error"]
    elif stock_days <= lead_time_days * 2:
        bar_color = COLORS["warning"]
    else:
        bar_color = COLORS["success"]

    short_label = oi.label.split(" - ")[0] if " - " in oi.label else oi.label
    days_txt = f"{stock_days:.0f} j" if stock_days else "N/A"

    with ui.column().classes("w-full gap-0 q-mb-sm"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(short_label).classes("text-caption").style(
                f"color: {COLORS['ink']}; font-weight: 500"
            )
            ui.label(days_txt).classes("text-caption").style(
                f"color: {bar_color}; font-weight: 700"
            )
        # Bar container
        ui.html(f"""
            <div style="
                position: relative;
                width: 100%;
                height: 20px;
                background: {COLORS['sage']};
                border-radius: 4px;
                overflow: hidden;
            ">
                <div style="
                    width: {pct_stock:.1f}%;
                    height: 100%;
                    background: {bar_color};
                    border-radius: 4px;
                    opacity: 0.8;
                    transition: width 0.5s ease;
                "></div>
                <div style="
                    position: absolute;
                    top: 0;
                    left: {pct_lead:.1f}%;
                    width: 2px;
                    height: 100%;
                    background: {COLORS['ink']};
                    opacity: 0.6;
                " title="Délai livraison ({lead_time_days} j)"></div>
            </div>
        """)
