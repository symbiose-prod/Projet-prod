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
from ui.theme import COLORS, kpi_card, page_layout

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


def _q_badge_color(days: float | None) -> str:
    """Quasar color name for badge."""
    if days is None:
        return "grey-6"
    if days < 14:
        return "red-6"
    if days < 30:
        return "amber-8"
    return "green-7"


def _format_number(n: float, unit: str = "") -> str:
    s = f"{n:,.0f}".replace(",", "\u202f")  # espace fine insécable
    return f"{s} {unit}".strip() if unit else s


def _short_label(label: str) -> str:
    """'Bouteille - 0.33L' → 'Bouteille 33cl', 'Bouteille 75cl SAFT - 0.75L' → 'Bouteille 75cl SAFT'."""
    if " - " not in label:
        return label
    name, vol = label.split(" - ", 1)
    # Si le nom contient déjà une taille (ex: "75cl"), on le garde tel quel
    if "cl" in name.lower():
        return name
    # Sinon, convertir le suffixe "0.33L" → "33cl"
    vol = vol.strip().rstrip("Ll")
    try:
        cl = int(float(vol) * 100)
        return f"{name} {cl}cl"
    except ValueError:
        return name


_MONTHS_FR = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def _format_date_fr(d) -> str:
    if d is None:
        return "—"
    return f"{d.day} {_MONTHS_FR[d.month]} {d.year}"


_URGENCY_COLORS = {
    "critical": COLORS["error"],
    "warning": COLORS["warning"],
    "ok": COLORS["success"],
}
_URGENCY_LABELS = {
    "critical": "URGENT — Commander maintenant",
    "warning": "A planifier",
    "ok": "Stock suffisant",
}
_URGENCY_ICONS = {
    "critical": "error",
    "warning": "schedule",
    "ok": "check_circle",
}


def _q_urgency_color(urgency: str) -> str:
    return {"critical": "red-6", "warning": "amber-8", "ok": "green-7"}[urgency]


# ─── Page ─────────────────────────────────────────────────────────────────────


@ui.page("/stocks")
def page_stocks():
    user = require_auth()
    if not user:
        return

    # Charger la config fournisseurs
    stocks_cfg = get_stocks_config()
    supplier_groups = stocks_cfg.get("supplier_groups") or []
    supplier_options = [g["name"] for g in supplier_groups]
    _analysable = {
        g["name"] for g in supplier_groups if g.get("patterns")
    }
    # Map supplier name → config dict (for icon lookup etc.)
    _supplier_cfg = {g["name"]: g for g in supplier_groups}

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
                selected_supplier = {"value": None}
                supplier_buttons: dict[str, ui.button] = {}

                def select_supplier(name: str):
                    prev = selected_supplier["value"]
                    if prev and prev in supplier_buttons:
                        supplier_buttons[prev].props(
                            "color=grey-8", remove="color=green-8"
                        )
                        supplier_buttons[prev].classes(remove="nav-active")
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

        # ── Placeholder "bientôt disponible" ──────────────────────────
        coming_soon_card = ui.card().classes("w-full").props("flat bordered")
        coming_soon_card.set_visibility(False)
        with coming_soon_card:
            with ui.card_section().classes("q-pa-lg text-center"):
                ui.icon("construction", size="xl").style(
                    f"color: {COLORS['warning']}; opacity: 0.6"
                )
                coming_soon_name = ui.label("").classes("text-h6 q-mt-sm")
                ui.label(
                    "L'analyse des stocks pour ce fournisseur "
                    "sera bientôt disponible."
                ).classes("text-body2 q-mt-xs").style(
                    f"color: {COLORS['ink2']}"
                )

        # ── Bloc d'analyse (masqué par défaut) ──────────────────────
        analysis_card = ui.column().classes("w-full gap-0")
        analysis_card.set_visibility(False)

        with analysis_card:
            # ── Header fournisseur ────────────────────────────────
            with ui.row().classes("items-center gap-3 q-mb-md"):
                supplier_icon_el = ui.icon(
                    "inventory_2", size="md",
                ).style(f"color: {COLORS['green']}")
                supplier_header = ui.label(
                    "Fournisseur",
                ).classes("text-h5").style(
                    f"color: {COLORS['ink']}; font-weight: 700"
                )

            # ── Layout 2 colonnes : résultats + panneau sticky ───
            with ui.row().classes("w-full items-start gap-3").style(
                "flex-wrap: nowrap"
            ):
                # Colonne gauche : résultats (prend tout l'espace)
                with ui.column().classes("gap-0").style(
                    "flex: 1 1 0; min-width: 0; overflow: hidden"
                ):
                    status_label = ui.label("").classes("text-body2")
                    status_label.set_visibility(False)

                    fetch_spinner = ui.spinner(
                        "dots", size="xl", color="green-8",
                    ).classes("self-center q-pa-md")
                    fetch_spinner.set_visibility(False)

                    results_container = ui.column().classes("w-full gap-0")

                # Colonne droite : contrôles sticky
                with ui.column().style(
                    "position: sticky; top: 16px; "
                    "width: 300px; min-width: 300px; flex: 0 0 300px;"
                ):
                    with ui.card().props("flat bordered").style(
                        "width: 300px; height: 300px"
                    ):
                        with ui.card_section().classes(
                            "q-pa-lg column justify-center items-center"
                        ).style("height: 100%"):
                            ui.label("Période d'analyse").classes(
                                "text-subtitle2 q-mb-md"
                            ).style(
                                f"color: {COLORS['ink']}; font-weight: 600"
                            )
                            period_radio = ui.radio(
                                {30: "1 mois", 60: "2 mois",
                                 90: "3 mois", 180: "6 mois"},
                                value=60,
                            ).props("dense color=green-8").style(
                                "font-size: 14px"
                            )
                            fetch_btn = ui.button(
                                "Analyser",
                                icon="analytics",
                                on_click=lambda: do_fetch(),
                            ).props(
                                "color=green-8 unelevated"
                            ).classes("w-full q-mt-lg").style(
                                "font-size: 15px; padding: 10px 16px"
                            )

            async def do_fetch():
                fetch_btn.disable()
                fetch_spinner.set_visibility(True)
                status_label.set_visibility(False)
                results_container.clear()
                try:
                    days = int(period_radio.value or 60)
                    selected = selected_supplier["value"]
                    groups: list[StockGroup] = await asyncio.wait_for(
                        asyncio.to_thread(fetch_and_compute, days),
                        timeout=60,
                    )
                    filtered = [g for g in groups if g.name == selected]
                    total_items = sum(len(g.items) for g in filtered)
                    if not filtered or total_items == 0:
                        status_label.text = (
                            f"Aucun contenant trouvé pour {selected}."
                        )
                        status_label.classes(
                            "text-negative", remove="text-positive"
                        )
                        status_label.set_visibility(True)
                        return
                    ordering_cfgs = {
                        g["name"]: g.get("ordering", {})
                        for g in supplier_groups
                        if g.get("ordering")
                    }
                    _render_results(
                        results_container, filtered, days, ordering_cfgs,
                    )
                    ui.notify("Analyse terminée", type="positive")
                except TimeoutError:
                    status_label.text = (
                        "L'analyse a dépassé le délai (60 s). Réessayez."
                    )
                    status_label.classes(
                        "text-negative", remove="text-positive"
                    )
                    status_label.set_visibility(True)
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

        # ── Callback sélection fournisseur ────────────────────────
        def _on_supplier_selected(name: str | None):
            placeholder_msg.set_visibility(False)
            analysis_card.set_visibility(False)
            coming_soon_card.set_visibility(False)

            if not name:
                placeholder_msg.set_visibility(True)
            elif name in _analysable:
                analysis_card.set_visibility(True)
                supplier_header.text = name
                cfg = _supplier_cfg.get(name, {})
                supplier_icon_el.props(f'name="{cfg.get("icon", "inventory_2")}"')
                results_container.clear()
                status_label.set_visibility(False)
            else:
                coming_soon_card.set_visibility(True)
                coming_soon_name.text = name


# ─── Rendu des résultats (sans expansion panel) ─────────────────────────────


def _render_results(
    container: ui.column,
    groups: list[StockGroup],
    window_days: int,
    ordering_cfgs: dict[str, dict],
) -> None:
    """Render stock analysis results — flat layout, no expansion panels."""
    with container:
        for group in groups:
            ordering = ordering_cfgs.get(group.name, {})

            # ── AUTONOMIE ─────────────────────────────────────────
            with ui.element("div").classes("w-full q-mt-lg q-mb-sm"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("timer", size="sm").style(
                        f"color: {COLORS['green']}"
                    )
                    ui.label("Autonomie des stocks").classes("text-h6").style(
                        f"color: {COLORS['ink']}; font-weight: 700"
                    )

            # KPI cards
            with ui.row().classes("w-full gap-4 flex-wrap"):
                for item in group.items:
                    kpi_card(
                        icon="inventory_2",
                        label=_short_label(item.label),
                        value=_format_days(item.stock_days),
                        color=_days_color(item.stock_days),
                    )

            # ── DETAIL TABLE ──────────────────────────────────────
            with ui.element("div").classes("w-full q-mt-lg q-mb-sm"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("table_chart", size="sm").style(
                        f"color: {COLORS['ink2']}"
                    )
                    ui.label("Détail").classes("text-subtitle1").style(
                        f"color: {COLORS['ink']}; font-weight: 600"
                    )

            columns = [
                {"name": "label", "label": "Contenant", "field": "label",
                 "align": "left", "sortable": True},
                {"name": "stock", "label": "Stock actuel", "field": "stock",
                 "align": "right"},
                {"name": "conso", "label": f"Conso ({window_days} j)",
                 "field": "conso", "align": "right"},
                {"name": "daily", "label": "Conso / jour", "field": "daily",
                 "align": "right"},
                {"name": "days", "label": "Autonomie", "field": "days",
                 "align": "right", "sortable": True},
            ]
            rows = []
            for item in group.items:
                rows.append({
                    "label": _short_label(item.label),
                    "stock": _format_number(item.current_stock, item.unit),
                    "conso": _format_number(item.consumption, item.unit),
                    "daily": f"{item.daily_consumption:,.1f} {item.unit}/j",
                    "days": _format_days(item.stock_days),
                    "_days_raw": item.stock_days,
                })
            table = ui.table(
                columns=columns,
                rows=rows,
                row_key="label",
            ).classes("w-full").props("flat bordered dense")

            # Slot custom pour colorer la colonne Autonomie
            table.add_slot(
                "body-cell-days",
                """
                <q-td :props="props" class="text-right">
                    <q-badge
                        :color="props.row._days_raw == null ? 'grey-5'
                              : props.row._days_raw < 14 ? 'red-6'
                              : props.row._days_raw < 30 ? 'amber-8'
                              : 'green-7'"
                        :label="props.row.days"
                        class="text-weight-bold"
                        style="font-size: 13px; padding: 4px 10px"
                    />
                </q-td>
                """,
            )

            # ── RECOMMANDATION DE COMMANDE ────────────────────────
            rec = compute_order_recommendation(group, ordering)
            if rec:
                _render_order_section(rec)


# ─── Section commande — redesign ────────────────────────────────────────────


def _render_order_section(rec: OrderRecommendation) -> None:
    """Render ordering recommendation as a prominent, clear section."""
    color = _URGENCY_COLORS[rec.urgency]
    q_color = _q_urgency_color(rec.urgency)

    # ── Section header ────────────────────────────────────────
    with ui.element("div").classes("w-full q-mt-xl q-mb-sm"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("local_shipping", size="sm").style(f"color: {color}")
            ui.label("Recommandation de commande").classes("text-h6").style(
                f"color: {COLORS['ink']}; font-weight: 700"
            )
            ui.badge(
                _URGENCY_LABELS[rec.urgency],
            ).props(f"color={q_color}")

    # ── Alert banner (urgence) ────────────────────────────────
    with ui.card().classes("w-full").props("flat").style(
        f"border-left: 4px solid {color}; "
        f"background: {color}08; "
        "border-radius: 6px;"
    ):
        with ui.card_section().classes("q-pa-md"):
            with ui.row().classes("items-center gap-3"):
                ui.icon(_URGENCY_ICONS[rec.urgency], size="md").style(
                    f"color: {color}"
                )
                with ui.column().classes("gap-0"):
                    if rec.urgency == "critical":
                        ui.label(
                            "Stock insuffisant pour couvrir le délai "
                            f"de livraison ({rec.lead_time_days} j)",
                        ).classes("text-body1").style("font-weight: 600")
                    elif rec.urgency == "warning":
                        ui.label(
                            f"Commander avant le "
                            f"{_format_date_fr(rec.order_deadline)}",
                        ).classes("text-body1").style("font-weight: 600")
                    else:
                        ui.label(
                            "Le stock actuel couvre largement le délai "
                            f"de livraison ({rec.lead_time_days} j)",
                        ).classes("text-body1").style("font-weight: 600")

    # ── Coverage bars ─────────────────────────────────────────
    with ui.card().classes("w-full q-mt-sm").props("flat bordered"):
        with ui.card_section().classes("q-pa-md"):
            ui.label("Couverture par référence").classes(
                "text-subtitle2 q-mb-md"
            ).style(f"color: {COLORS['ink']}; font-weight: 600")

            max_days = max(
                (oi.stock_days or 0 for oi in rec.items), default=60,
            )
            bar_max = max(max_days, rec.lead_time_days * 3, 60)

            for oi in rec.items:
                _render_coverage_bar(oi, rec.lead_time_days, bar_max)

    # ── Synthèse commande (tableau clair) ─────────────────────
    with ui.card().classes("w-full q-mt-sm").props("flat bordered"):
        with ui.card_section().classes("q-pa-md"):
            ui.label("Synthèse de commande").classes(
                "text-subtitle2 q-mb-md"
            ).style(f"color: {COLORS['ink']}; font-weight: 600")

            # Infos clés en row
            with ui.row().classes("w-full gap-6 q-mb-md flex-wrap"):
                _metric_chip("event", "Date limite",
                             _format_date_fr(rec.order_deadline), color)
                _metric_chip("schedule", "Délai",
                             f"{rec.lead_time_days} j", COLORS["ink2"])
                _metric_chip("local_shipping", "Min. palettes",
                             str(rec.min_pallets), COLORS["ink2"])

            # Tableau de répartition
            order_cols = [
                {"name": "ref", "label": "Référence", "field": "ref",
                 "align": "left"},
                {"name": "pallets", "label": "Palettes", "field": "pallets",
                 "align": "center"},
                {"name": "qty", "label": "Bouteilles", "field": "qty",
                 "align": "right"},
                {"name": "coverage", "label": "Couverture",
                 "field": "coverage", "align": "right"},
            ]
            order_rows = []
            for oi in rec.items:
                order_rows.append({
                    "ref": _short_label(oi.label),
                    "pallets": str(oi.suggested_pallets),
                    "qty": _format_number(oi.suggested_qty),
                    "coverage": (
                        f"~{oi.coverage_days:.0f} j"
                        if oi.coverage_days else "—"
                    ),
                })
            # Total row
            total_pal = sum(oi.suggested_pallets for oi in rec.items)
            total_qty = sum(oi.suggested_qty for oi in rec.items)
            order_rows.append({
                "ref": "TOTAL",
                "pallets": str(total_pal),
                "qty": _format_number(total_qty),
                "coverage": "",
            })
            ui.table(
                columns=order_cols,
                rows=order_rows,
                row_key="ref",
            ).classes("w-full").props("flat bordered dense")


def _metric_chip(icon: str, label: str, value: str, color: str) -> None:
    """Small metric display with icon."""
    with ui.row().classes("items-center gap-2"):
        with ui.element("div").classes("q-pa-xs").style(
            f"background: {color}10; border-radius: 6px"
        ):
            ui.icon(icon, size="xs").style(f"color: {color}")
        with ui.column().classes("gap-0"):
            ui.label(label).classes("text-caption").style(
                f"color: {COLORS['ink2']}; font-weight: 500; font-size: 11px"
            )
            ui.label(value).classes("text-body2").style(
                f"color: {COLORS['ink']}; font-weight: 700"
            )


def _render_coverage_bar(oi, lead_time_days: int, bar_max: float) -> None:
    """Render a horizontal coverage bar for one reference."""
    stock_days = oi.stock_days or 0
    pct_stock = min(stock_days / bar_max * 100, 100) if bar_max > 0 else 0
    pct_lead = min(lead_time_days / bar_max * 100, 100) if bar_max > 0 else 0

    if stock_days <= lead_time_days:
        bar_color = COLORS["error"]
    elif stock_days <= lead_time_days * 2:
        bar_color = COLORS["warning"]
    else:
        bar_color = COLORS["success"]

    short_label = _short_label(oi.label)
    days_txt = f"{stock_days:.0f} j" if stock_days else "N/A"

    with ui.column().classes("w-full gap-0 q-mb-md"):
        with ui.row().classes("items-center justify-between w-full q-mb-xs"):
            ui.label(short_label).classes("text-body2").style(
                f"color: {COLORS['ink']}; font-weight: 600"
            )
            ui.badge(days_txt).props(
                f"color={'red-6' if stock_days <= lead_time_days else 'amber-8' if stock_days <= lead_time_days * 2 else 'green-7'}"
            ).style("font-size: 12px")
        # Bar container
        ui.html(f"""
            <div style="
                position: relative; width: 100%; height: 24px;
                background: {COLORS['sage']}; border-radius: 6px;
                overflow: visible;
            ">
                <div style="
                    width: {pct_stock:.1f}%; height: 100%;
                    background: {bar_color}; border-radius: 6px;
                    opacity: 0.75; transition: width 0.5s ease;
                "></div>
                <div style="
                    position: absolute; top: -4px;
                    left: {pct_lead:.1f}%; width: 2px; height: 32px;
                    background: {COLORS['ink']}; opacity: 0.5;
                    border-radius: 1px;
                "></div>
                <div style="
                    position: absolute; top: -18px;
                    left: calc({pct_lead:.1f}% - 20px);
                    font-size: 10px; color: {COLORS['ink2']};
                    white-space: nowrap;
                ">{lead_time_days}j</div>
            </div>
        """)
