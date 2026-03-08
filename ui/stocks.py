"""
ui/stocks.py
============
Page Stocks — Analyse de l'autonomie des stocks contenants, groupés par fournisseur.
Sélection du fournisseur dans la sidebar, puis analyse par période.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date as _date

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
            # Responsive : côte à côte sur desktop, empilé sur mobile
            with ui.row().classes("w-full items-start gap-4").style(
                "flex-wrap: wrap"
            ):
                # Colonne gauche : résultats (prend tout l'espace)
                with ui.column().classes("gap-0").style(
                    "flex: 1 1 600px; min-width: 0; overflow: hidden"
                ):
                    status_label = ui.label("").classes("text-body2")
                    status_label.set_visibility(False)

                    fetch_spinner = ui.spinner(
                        "dots", size="xl", color="green-8",
                    ).classes("self-center q-pa-md")
                    fetch_spinner.set_visibility(False)

                    results_container = ui.column().classes("w-full gap-0")

                # Colonne droite : contrôles sticky
                # flex-basis 220px → prend sa place naturelle, sticky sur desktop
                with ui.column().style(
                    "position: sticky; top: 16px; "
                    "flex: 0 1 auto;"
                ):
                    with ui.card().props("flat bordered"):
                        with ui.card_section().classes(
                            "q-pa-lg column items-center"
                        ):
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

    # ── Bouton "Préparer la commande" (si IA configurée) ────
    from common.ai import is_ai_configured

    if is_ai_configured():
        ui.button(
            "Préparer la commande",
            icon="email",
            on_click=lambda: _open_order_dialog(rec),
        ).props("color=green-8 unelevated").classes("q-mt-md").style(
            "font-size: 14px"
        )


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


# ─── Dialog commande assisté par IA ──────────────────────────────────────────


async def _open_order_dialog(rec: OrderRecommendation) -> None:
    """Open full-screen dialog: left=order summary, right=AI chat, bottom=actions."""
    from common.ai import generate_order_email
    from common.easybeer.suppliers import (
        extract_supplier_address,
        extract_supplier_contact_name,
        extract_supplier_email,
        find_fournisseur_by_name,
    )
    from common.email import send_html_with_pdf
    from common.xlsx_fill.bon_commande_pdf import build_bon_commande_pdf

    # ── State ─────────────────────────────────────────────────────────────
    state: dict = {
        "conversation": [],
        "current_draft": "",
        "subject": f"Commande — {rec.supplier}",
        "supplier_email": None,
        "supplier_info": None,
        "loading": False,
        "language": "fr",
        "delivery_mode": "asap",
        "delivery_date": None,
    }

    # ── Dialog ────────────────────────────────────────────────────────────
    with ui.dialog().props("maximized persistent") as dlg, \
         ui.card().classes("w-full h-full q-pa-none").style(
             "display: flex; flex-direction: column"
         ):

        # ── Top bar ──
        with ui.row().classes(
            "w-full items-center q-pa-md gap-3"
        ).style(
            f"background: {COLORS['green']}; color: white; flex-shrink: 0"
        ):
            ui.icon("email", size="sm")
            ui.label(f"Commande — {rec.supplier}").classes(
                "text-h6"
            ).style("font-weight: 600")
            ui.element("div").style("flex-grow: 1")
            ui.button(icon="close", on_click=dlg.close).props(
                "flat round color=white"
            )

        # ── Main content: 2 panels ──
        with ui.row().classes("w-full items-start").style(
            "flex: 1 1 0; overflow: hidden"
        ):
            # LEFT panel: order summary (no scroll — all visible)
            with ui.column().style(
                f"width: 380px; border-right: 1px solid "
                f"{COLORS.get('border', '#e5e7eb')}; overflow-y: auto"
            ):
                with ui.column().classes("q-pa-md gap-2"):
                    _render_order_summary_panel(rec)

                    # ── Options ──
                    ui.separator().classes("q-my-md")
                    ui.label("Options").classes("text-subtitle2").style(
                        f"color: {COLORS['ink']}; font-weight: 600"
                    )

                    # Language — two separate buttons
                    ui.label("Langue de l'email").classes("text-caption").style(
                        f"color: {COLORS['ink2']}"
                    )
                    with ui.row().classes("gap-3"):
                        btn_fr = ui.button(
                            "🇫🇷 Français",
                        ).props("unelevated no-caps color=green-8").style(
                            "min-width: 130px"
                        )
                        btn_en = ui.button(
                            "🇬🇧 English",
                        ).props("outline no-caps color=grey-6").style(
                            "min-width: 130px"
                        )

                    def _set_lang(lang: str):
                        state["language"] = lang
                        if lang == "fr":
                            btn_fr.props("unelevated color=green-8")
                            btn_fr.props(remove="outline")
                            btn_en.props("outline color=grey-6")
                            btn_en.props(remove="unelevated")
                        else:
                            btn_en.props("unelevated color=green-8")
                            btn_en.props(remove="outline")
                            btn_fr.props("outline color=grey-6")
                            btn_fr.props(remove="unelevated")

                    btn_fr.on_click(lambda: _set_lang("fr"))
                    btn_en.on_click(lambda: _set_lang("en"))

                    # Delivery preference — two separate buttons
                    ui.label("Livraison souhaitée").classes(
                        "text-caption q-mt-sm"
                    ).style(f"color: {COLORS['ink2']}")
                    with ui.row().classes("gap-3"):
                        btn_asap = ui.button(
                            "ASAP",
                        ).props("unelevated no-caps color=green-8").style(
                            "min-width: 130px"
                        )
                        btn_date = ui.button(
                            "📅 Date précise",
                        ).props("outline no-caps color=grey-6").style(
                            "min-width: 130px"
                        )

                    delivery_date_input = ui.input(
                        "Date souhaitée",
                    ).props(
                        "outlined dense"
                    ).classes("w-full").style("display: none")
                    with delivery_date_input:
                        with ui.menu().props("no-parent-event") as date_menu:
                            with ui.date().props(
                                "mask=DD/MM/YYYY"
                            ).bind_value(delivery_date_input) as date_picker:
                                pass
                        with delivery_date_input.add_slot("append"):
                            ui.icon("event", size="sm").on(
                                "click", date_menu.open
                            ).classes("cursor-pointer")

                    def _set_delivery(mode: str):
                        state["delivery_mode"] = mode
                        if mode == "asap":
                            btn_asap.props("unelevated color=green-8")
                            btn_asap.props(remove="outline")
                            btn_date.props("outline color=grey-6")
                            btn_date.props(remove="unelevated")
                            delivery_date_input.style("display: none")
                            state["delivery_date"] = None
                        else:
                            btn_date.props("unelevated color=green-8")
                            btn_date.props(remove="outline")
                            btn_asap.props("outline color=grey-6")
                            btn_asap.props(remove="unelevated")
                            delivery_date_input.style("display: block")

                    btn_asap.on_click(lambda: _set_delivery("asap"))
                    btn_date.on_click(lambda: _set_delivery("date"))
                    delivery_date_input.on_value_change(
                        lambda e: state.update({"delivery_date": e.value})
                    )

            # RIGHT panel: chat
            with ui.column().style(
                "flex: 1 1 0; display: flex; flex-direction: column; "
                "overflow: hidden; height: 100%"
            ):
                # Chat messages area
                chat_scroll = ui.scroll_area().style(
                    "flex: 1 1 0; overflow-y: auto"
                )
                with chat_scroll:
                    chat_container = ui.column().classes("w-full gap-2 q-pa-md")

                # Input area
                with ui.row().classes(
                    "w-full q-pa-sm items-end gap-2"
                ).style(
                    f"border-top: 1px solid {COLORS.get('border', '#e5e7eb')}; "
                    "flex-shrink: 0"
                ):
                    msg_input = ui.textarea(
                        placeholder="Demander une modification...",
                    ).props("outlined dense autogrow").classes(
                        "flex-1"
                    ).style("max-height: 120px")
                    chat_send_btn = ui.button(
                        icon="send",
                        on_click=lambda: _send_chat_msg(),
                    ).props("color=green-8 round unelevated")

        # ── Bottom action bar ──
        with ui.row().classes(
            "w-full q-pa-md justify-end gap-3"
        ).style(
            f"border-top: 1px solid {COLORS.get('border', '#e5e7eb')}; "
            "flex-shrink: 0"
        ):
            supplier_email_label = ui.label("").classes(
                "text-caption self-center"
            ).style(f"color: {COLORS['ink2']}")
            ui.element("div").style("flex-grow: 1")
            preview_pdf_btn = ui.button(
                "Aperçu PDF", icon="picture_as_pdf",
            ).props("outline color=grey-8")
            preview_btn = ui.button(
                "Aperçu email", icon="visibility",
            ).props("outline color=grey-8")
            send_email_btn = ui.button(
                "Envoyer la commande", icon="send",
            ).props("color=green-8 unelevated")

    # ── Chat logic ────────────────────────────────────────────────────────

    async def _init_chat():
        """Fetch supplier info from EasyBeer, then generate initial draft."""
        # 1. Fetch supplier from EasyBeer
        try:
            fournisseur = await asyncio.wait_for(
                asyncio.to_thread(find_fournisseur_by_name, rec.supplier),
                timeout=15,
            )
            if fournisseur:
                state["supplier_info"] = fournisseur
                state["supplier_email"] = extract_supplier_email(fournisseur)
                if state["supplier_email"]:
                    supplier_email_label.text = (
                        f"Destinataire : {state['supplier_email']}"
                    )
                else:
                    supplier_email_label.text = (
                        "Email fournisseur introuvable dans EasyBeer"
                    )
                    supplier_email_label.style(f"color: {COLORS['error']}")
        except Exception:
            _log.warning("Could not fetch supplier info for %s", rec.supplier)
            supplier_email_label.text = "Impossible de charger la fiche fournisseur"

        # 2. Show loading message
        with chat_container:
            loading_msg = ui.chat_message(
                "Génération du brouillon en cours...",
                name="Ferment AI",
                avatar="🤖",
            )

        # 3. Build context and generate
        context = {
            "supplier_name": rec.supplier,
            "supplier_email": state["supplier_email"],
            "items": [
                {
                    "label": _short_label(oi.label),
                    "suggested_pallets": oi.suggested_pallets,
                    "suggested_qty": oi.suggested_qty,
                    "bottles_per_pallet": oi.bottles_per_pallet,
                    "coverage_days": oi.coverage_days,
                }
                for oi in rec.items
            ],
            "lead_time_days": rec.lead_time_days,
            "order_deadline": _format_date_fr(rec.order_deadline),
            "urgency": rec.urgency,
            "language": state["language"],
            "delivery_preference": state["delivery_mode"],
            "delivery_date_requested": state.get("delivery_date") or "",
        }

        try:
            draft = await asyncio.wait_for(
                asyncio.to_thread(generate_order_email, context),
                timeout=45,
            )
            # Parse subject from first line "Objet : ..."
            lines = draft.split("\n", 1)
            if lines[0].lower().startswith(("objet", "subject")):
                state["subject"] = (
                    lines[0]
                    .replace("Objet :", "")
                    .replace("Objet:", "")
                    .replace("Subject:", "")
                    .replace("Subject :", "")
                    .strip()
                )
                state["current_draft"] = lines[1].strip() if len(lines) > 1 else ""
            else:
                state["current_draft"] = draft

            state["conversation"] = [
                {"role": "user", "content": _build_context_prompt(context)},
                {"role": "assistant", "content": draft},
            ]

            # Replace loading message with actual draft
            chat_container.clear()
            with chat_container:
                ui.chat_message(
                    state["current_draft"],
                    name="Ferment AI",
                    avatar="🤖",
                    text_html=True,
                )
            chat_scroll.scroll_to(percent=1.0)

        except Exception as exc:
            _log.exception("Error generating initial draft")
            chat_container.clear()
            with chat_container:
                ui.chat_message(
                    f"Erreur lors de la génération : {exc}",
                    name="Erreur",
                    avatar="⚠️",
                )

    async def _send_chat_msg():
        """Send user refinement message to Claude."""
        user_msg = msg_input.value.strip()
        if not user_msg or state["loading"]:
            return
        state["loading"] = True
        chat_send_btn.disable()
        msg_input.value = ""

        # Show user message
        with chat_container:
            ui.chat_message(user_msg, name="Vous", sent=True)
        chat_scroll.scroll_to(percent=1.0)

        # Add to conversation
        state["conversation"].append({"role": "user", "content": user_msg})

        # Show typing indicator
        with chat_container:
            typing_el = ui.chat_message(
                "Mise à jour en cours...",
                name="Ferment AI",
                avatar="🤖",
            )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    generate_order_email,
                    {},
                    state["conversation"],
                ),
                timeout=45,
            )
            state["conversation"].append(
                {"role": "assistant", "content": response}
            )

            # Parse subject if present
            lines = response.split("\n", 1)
            if lines[0].lower().startswith(("objet", "subject")):
                state["subject"] = (
                    lines[0]
                    .replace("Objet :", "")
                    .replace("Objet:", "")
                    .replace("Subject:", "")
                    .replace("Subject :", "")
                    .strip()
                )
                state["current_draft"] = (
                    lines[1].strip() if len(lines) > 1 else ""
                )
            else:
                state["current_draft"] = response

            # Replace typing indicator with actual response
            chat_container.remove(typing_el)
            with chat_container:
                ui.chat_message(
                    state["current_draft"],
                    name="Ferment AI",
                    avatar="🤖",
                    text_html=True,
                )
            chat_scroll.scroll_to(percent=1.0)

        except Exception as exc:
            _log.exception("Error refining draft")
            state["conversation"].pop()  # remove failed user msg
            chat_container.remove(typing_el)
            with chat_container:
                ui.chat_message(
                    f"Erreur : {exc}",
                    name="Erreur",
                    avatar="⚠️",
                )
        finally:
            state["loading"] = False
            chat_send_btn.enable()

    async def _preview_email():
        """Show email preview in a sub-dialog."""
        if not state["current_draft"]:
            ui.notify("Aucun brouillon disponible", type="warning")
            return

        with ui.dialog() as preview_dlg, ui.card().classes("q-pa-lg").style(
            "width: 700px; max-width: 90vw; max-height: 80vh; overflow-y: auto"
        ):
            ui.label(f"Objet : {state['subject']}").classes("text-subtitle1")
            dest = state["supplier_email"] or "Non trouvé"
            ui.label(f"À : {dest}").classes("text-body2").style(
                f"color: {COLORS['ink2']}"
            )
            ui.label(
                "CC : maxime@symbiose-kefir.fr, nicolas@symbiose-kefir.fr"
            ).classes("text-body2").style(f"color: {COLORS['ink2']}")
            ui.separator().classes("q-my-sm")
            ui.html(state["current_draft"]).style("font-size: 14px")
            ui.separator().classes("q-my-sm")
            ui.label("📎 Bon de commande PDF joint").classes(
                "text-caption"
            ).style(f"color: {COLORS['ink2']}")
            with ui.row().classes("w-full justify-end q-mt-md gap-2"):
                ui.button("Fermer", on_click=preview_dlg.close).props(
                    "flat color=grey-7"
                )
        preview_dlg.open()

    async def _send_order_email():
        """Build PDF and send via Brevo."""
        recipient = state["supplier_email"]
        if not recipient:
            ui.notify(
                "Email fournisseur introuvable. Vérifiez dans EasyBeer.",
                type="negative",
            )
            return
        if not state["current_draft"]:
            ui.notify("Aucun brouillon à envoyer.", type="warning")
            return

        send_email_btn.disable()
        ui.notify("Envoi en cours...", type="info", icon="hourglass_empty")

        try:
            # Build PDF
            today = _date.today()
            supplier_short = (
                rec.supplier.upper().replace(" ", "").replace("-", "")[:8]
            )
            ref = f"BC-{today.strftime('%Y-%m%d')}-{supplier_short}"

            pdf_items = [
                {
                    "label": _short_label(oi.label),
                    "pallets": oi.suggested_pallets,
                    "qty": oi.suggested_qty,
                    "conditionnement": f"{oi.bottles_per_pallet}/palette",
                }
                for oi in rec.items
            ]

            supplier_info_dict: dict = {
                "name": rec.supplier,
                "address_lines": [],
                "contact_name": None,
                "email": recipient,
            }
            if state["supplier_info"]:
                supplier_info_dict["address_lines"] = extract_supplier_address(
                    state["supplier_info"]
                )
                supplier_info_dict["contact_name"] = (
                    extract_supplier_contact_name(state["supplier_info"])
                )

            pdf_data = {
                "reference": ref,
                "date": today,
                "items": pdf_items,
                "delivery_date": _format_date_fr(rec.order_deadline),
                "notes": None,
            }

            pdf_bytes = await asyncio.to_thread(
                build_bon_commande_pdf, pdf_data, supplier_info_dict,
            )

            # Build full HTML email with signature
            html_body = state["current_draft"]
            html_body += (
                "<hr>"
                "<p><strong>Ferment Station</strong><br>"
                "Producteur de boissons fermentées bio<br>"
                "47 rue Ernest Renan — 94200 Ivry-sur-Seine<br>"
                "Tél : 09 67 50 46 47</p>"
            )

            # Send
            filename = f"Bon_Commande_{ref}.pdf"

            cc_list = [
                "maxime@symbiose-kefir.fr",
                "nicolas@symbiose-kefir.fr",
            ]

            def _do_send():
                send_html_with_pdf(
                    to_email=recipient,
                    subject=state["subject"],
                    html_body=html_body,
                    attachments=[(filename, pdf_bytes)],
                    cc=cc_list,
                )

            await asyncio.to_thread(_do_send)

            cc_text = ", ".join(cc_list)
            ui.notify(
                f"Commande envoyée à {recipient} (CC: {cc_text})",
                type="positive",
                icon="check_circle",
            )
            dlg.close()

        except Exception as exc:
            _log.exception("Error sending order email")
            ui.notify(f"Erreur d'envoi : {exc}", type="negative")
        finally:
            send_email_btn.enable()

    async def _preview_pdf():
        """Generate and display the PDF purchase order in a sub-dialog."""
        import base64

        preview_pdf_btn.disable()
        try:
            today = _date.today()
            supplier_short = (
                rec.supplier.upper().replace(" ", "").replace("-", "")[:8]
            )
            ref = f"BC-{today.strftime('%Y-%m%d')}-{supplier_short}"

            pdf_items = [
                {
                    "label": _short_label(oi.label),
                    "pallets": oi.suggested_pallets,
                    "qty": oi.suggested_qty,
                    "conditionnement": f"{oi.bottles_per_pallet}/palette",
                }
                for oi in rec.items
            ]

            supplier_info_dict: dict = {
                "name": rec.supplier,
                "address_lines": [],
                "contact_name": None,
                "email": state["supplier_email"],
            }
            if state["supplier_info"]:
                supplier_info_dict["address_lines"] = extract_supplier_address(
                    state["supplier_info"]
                )
                supplier_info_dict["contact_name"] = (
                    extract_supplier_contact_name(state["supplier_info"])
                )

            delivery = state.get("delivery_date") or _format_date_fr(
                rec.order_deadline
            )
            if state["delivery_mode"] == "asap":
                delivery = "Dès que possible (ASAP)"

            pdf_data = {
                "reference": ref,
                "date": today,
                "items": pdf_items,
                "delivery_date": delivery,
                "notes": None,
            }

            pdf_bytes = await asyncio.to_thread(
                build_bon_commande_pdf, pdf_data, supplier_info_dict,
            )

            # Encode to base64 for inline display
            b64 = base64.b64encode(pdf_bytes).decode("ascii")

            with ui.dialog() as pdf_dlg, ui.card().classes("q-pa-none").style(
                "width: 900px; max-width: 95vw; height: 85vh"
            ):
                with ui.row().classes(
                    "w-full items-center q-pa-sm"
                ).style(
                    f"background: {COLORS.get('bg2', '#f9fafb')}; "
                    "flex-shrink: 0"
                ):
                    ui.label(f"📄 {ref}").classes("text-subtitle2").style(
                        "font-weight: 600"
                    )
                    ui.element("div").style("flex-grow: 1")
                    ui.button("Fermer", on_click=pdf_dlg.close).props(
                        "flat color=grey-7"
                    )
                ui.html(
                    f'<iframe src="data:application/pdf;base64,{b64}" '
                    f'style="width:100%; height:100%; border:none"></iframe>'
                ).style("flex: 1 1 0; overflow: hidden")
            pdf_dlg.open()

        except Exception as exc:
            _log.exception("Error generating PDF preview")
            ui.notify(f"Erreur PDF : {exc}", type="negative")
        finally:
            preview_pdf_btn.enable()

    # ── Wire up button handlers ──
    preview_pdf_btn.on_click(_preview_pdf)
    preview_btn.on_click(_preview_email)
    send_email_btn.on_click(_send_order_email)

    # ── Open and start ──
    dlg.open()
    await _init_chat()


def _build_context_prompt(context: dict) -> str:
    """Build initial Claude prompt from order context (used internally)."""
    from common.ai import _build_initial_prompt
    return _build_initial_prompt(context)


def _render_order_summary_panel(rec: OrderRecommendation) -> None:
    """Render read-only order summary in the left panel of the dialog."""
    ui.label("Résumé de la commande").classes("text-subtitle1").style(
        f"color: {COLORS['ink']}; font-weight: 700"
    )

    with ui.column().classes("gap-2 q-mt-md"):
        _summary_row("Fournisseur", rec.supplier)
        _summary_row(
            "Urgence",
            _URGENCY_LABELS[rec.urgency],
            color=_URGENCY_COLORS[rec.urgency],
        )
        _summary_row("Délai livraison", f"{rec.lead_time_days} jours")
        _summary_row("Date limite", _format_date_fr(rec.order_deadline))
        _summary_row("Min. palettes", str(rec.min_pallets))

    ui.separator().classes("q-my-md")
    ui.label("Articles").classes("text-subtitle2").style(
        f"color: {COLORS['ink']}; font-weight: 600"
    )

    for oi in rec.items:
        with ui.card().classes("w-full q-pa-sm q-mt-xs").props("flat bordered"):
            ui.label(_short_label(oi.label)).classes("text-body2").style(
                "font-weight: 600"
            )
            with ui.row().classes("gap-4"):
                ui.label(
                    f"{oi.suggested_pallets} pal."
                ).classes("text-caption")
                ui.label(
                    _format_number(oi.suggested_qty)
                ).classes("text-caption")
                if oi.coverage_days:
                    ui.label(
                        f"~{oi.coverage_days:.0f} j"
                    ).classes("text-caption").style(
                        f"color: {COLORS['ink2']}"
                    )

    total_pal = sum(oi.suggested_pallets for oi in rec.items)
    total_qty = sum(oi.suggested_qty for oi in rec.items)
    ui.separator().classes("q-my-sm")
    with ui.row().classes("justify-between w-full"):
        ui.label("TOTAL").style(
            f"color: {COLORS['ink']}; font-weight: 700"
        )
        ui.label(
            f"{total_pal} pal. / {_format_number(total_qty)} u"
        ).style(f"color: {COLORS['ink']}; font-weight: 700")


def _summary_row(label: str, value: str, *, color: str | None = None) -> None:
    """Label-value row in the order summary panel."""
    with ui.row().classes("w-full justify-between items-center"):
        ui.label(label).classes("text-caption").style(
            f"color: {COLORS['ink2']}"
        )
        lbl = ui.label(value).classes("text-body2").style("font-weight: 600")
        if color:
            lbl.style(f"color: {color}")
