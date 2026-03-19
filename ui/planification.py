"""
ui/planification.py
===================
Page Planification — Approvisionnement prévisionnel basé sur les brassins planifiés.

Affiche les brassins planifiés dans EasyBeer, leur répartition de conditionnement,
et l'impact sur les stocks de composants (ingrédients + emballages).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from nicegui import ui

_log = logging.getLogger("ferment.planification")

from ui.auth import require_auth
from ui.theme import COLORS, kpi_card, page_layout, section_title

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _badge_color(stock_after: float, current_stock: float) -> str:
    """Return badge color based on stock deficit."""
    if stock_after < 0:
        return "red"
    if current_stock > 0 and stock_after / current_stock < 0.2:
        return "orange"
    return "green"


def _fmt_qty(val: float, unit: str = "") -> str:
    """Format a quantity nicely."""
    if abs(val) >= 100:
        formatted = f"{val:,.0f}".replace(",", "\u202f")
    elif abs(val) >= 1:
        formatted = f"{val:,.1f}".replace(",", "\u202f")
    else:
        formatted = f"{val:.3f}"
    return f"{formatted} {unit}".strip()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@ui.page("/planification", response_timeout=30.0)
async def page_planification():
    user = require_auth()
    if not user:
        return

    # ── State ──
    brassins_state: dict[str, Any] = {"brassins": [], "needs": []}

    with page_layout("Planification", "event_note", "/planification") as sidebar:

        # ── Sidebar ──
        with sidebar:
            ui.label("Horizon").classes("text-subtitle2").style(
                f"color: {COLORS['ink2']}"
            )
            horizon_radio = ui.radio(
                {30: "1 mois", 60: "2 mois", 90: "3 mois"},
                value=90,
            ).props("dense")

            ui.element("div").style("height: 16px")

            fetch_btn = ui.button(
                "Analyser",
                icon="play_arrow",
                on_click=lambda: do_analyze(),
            ).props("color=green-8 unelevated").classes("full-width")

        # ── KPIs placeholder ──
        kpi_row = ui.row().classes("w-full gap-4 flex-wrap q-mb-md")
        with kpi_row:
            kpi_card("event", "Brassins planifiés", "—")
            kpi_card("local_drink", "Volume total", "—")
            kpi_card("warning", "Composants en déficit", "—")

        # ── Brassins section ──
        section_title("Brassins planifiés", "event_note")
        brassins_container = ui.column().classes("w-full gap-3")

        # ── Impact stocks section ──
        section_title("Impact sur les stocks", "inventory_2")
        needs_container = ui.column().classes("w-full gap-3")

        # ── Analyze function ──
        async def do_analyze():
            fetch_btn.disable()
            with brassins_container:
                spinner = ui.spinner("dots", size="lg", color="green")

            needs_container.clear()

            try:
                from ui._planification_calc import fetch_planning_data

                days = int(horizon_radio.value or 90)
                brassins, needs = await asyncio.wait_for(
                    asyncio.to_thread(fetch_planning_data, days),
                    timeout=120,
                )
                brassins_state["brassins"] = brassins
                brassins_state["needs"] = needs

                # Update KPIs
                total_vol = sum(b.volume for b in brassins)
                nb_deficit = sum(1 for n in needs if n.stock_after < 0)

                kpi_row.clear()
                with kpi_row:
                    kpi_card("event", "Brassins planifiés", str(len(brassins)))
                    kpi_card(
                        "local_drink", "Volume total",
                        f"{total_vol:,.0f} L".replace(",", "\u202f"),
                    )
                    kpi_card(
                        "warning", "Composants en déficit", str(nb_deficit),
                        color=COLORS["error"] if nb_deficit > 0 else COLORS["green"],
                    )

                # Render brassins
                _render_brassins(brassins_container, brassins, brassins_state)

                # Render needs
                _render_needs(needs_container, needs)

            except TimeoutError:
                ui.notify("Timeout — EasyBeer ne répond pas", type="negative")
            except Exception:
                _log.exception("Erreur analyse planification")
                ui.notify("Erreur lors de l'analyse", type="negative")
            finally:
                try:
                    spinner.delete()
                except Exception:
                    pass
                fetch_btn.enable()


# ---------------------------------------------------------------------------
# Render brassins
# ---------------------------------------------------------------------------

def _render_brassins(
    container: ui.column,
    brassins: list,
    state: dict,
):
    """Render the planned brassins list with expandable conditioning."""
    container.clear()

    if not brassins:
        with container:
            ui.label("Aucun brassin planifié sur cette période.").style(
                f"color: {COLORS['ink2']}"
            )
        return

    with container:
        for brassin in brassins:
            with ui.expansion(
                text="",
                icon="science",
            ).classes("w-full").props("dense header-class='text-weight-medium'").style(
                f"border: 1px solid {COLORS['border']}; border-radius: 8px"
            ) as exp:
                # Custom header content
                exp._props["label"] = (
                    f"{brassin.code}  —  {brassin.product_label}  "
                    f"—  {brassin.volume:,.0f} L  "
                    f"—  Conditionnement : {brassin.date_conditionnement or '?'}"
                )

                # Expansion content: conditioning lines
                with ui.column().classes("w-full q-pa-md gap-2"):
                    if not brassin.conditioning:
                        ui.label("Aucune ligne de conditionnement.").style(
                            f"color: {COLORS['ink2']}"
                        )
                    else:
                        # Table header
                        with ui.row().classes("w-full items-center gap-4").style(
                            f"border-bottom: 1px solid {COLORS['border']}; padding-bottom: 4px"
                        ):
                            ui.label("Produit").classes("text-caption text-weight-bold").style("width: 250px")
                            ui.label("Format").classes("text-caption text-weight-bold").style("width: 250px")
                            ui.label("Quantité").classes("text-caption text-weight-bold").style("width: 80px")
                            ui.label("Volume").classes("text-caption text-weight-bold").style("width: 100px")

                        for line in brassin.conditioning:
                            with ui.row().classes("w-full items-center gap-4"):
                                ui.label(line.product_label).style(
                                    f"width: 250px; color: {COLORS['ink']}"
                                )
                                ui.label(line.contenant_label).style(
                                    f"width: 250px; color: {COLORS['ink2']}"
                                )
                                ui.label(str(line.quantity)).style(
                                    f"width: 80px; color: {COLORS['ink']}"
                                )
                                ui.label(f"{line.volume:,.0f} L").style(
                                    f"width: 100px; color: {COLORS['ink2']}"
                                )

                    # Ingredients summary
                    if brassin.ingredients:
                        ui.separator().classes("q-my-sm")
                        ui.label("Ingrédients (recette)").classes(
                            "text-caption text-weight-bold"
                        ).style(f"color: {COLORS['ink2']}")
                        for ing in brassin.ingredients:
                            needed = ing["total_qty"]
                            if needed > 0:
                                ui.label(
                                    f"  {ing['label']}  —  "
                                    f"{_fmt_qty(needed, ing['unit'])}"
                                ).classes("text-caption").style(
                                    f"color: {COLORS['ink2']}"
                                )


# ---------------------------------------------------------------------------
# Render component needs
# ---------------------------------------------------------------------------

def _render_needs(container: ui.column, needs: list):
    """Render stock impact grouped by supplier."""
    container.clear()

    if not needs:
        with container:
            ui.label("Aucun besoin calculé.").style(f"color: {COLORS['ink2']}")
        return

    # Group by supplier
    groups: dict[str, list] = {}
    for need in needs:
        supplier = need.supplier or "Non attribué"
        groups.setdefault(supplier, []).append(need)

    with container:
        for supplier_name, supplier_needs in sorted(groups.items()):
            has_deficit = any(n.stock_after < 0 for n in supplier_needs)

            with ui.card().classes("w-full q-mb-sm").props("flat bordered").style(
                f"border: 1px solid {COLORS['border']}; border-radius: 8px"
            ):
                with ui.card_section().classes("q-pa-md"):
                    # Supplier header
                    with ui.row().classes("items-center gap-2 q-mb-sm"):
                        ui.icon(
                            "local_shipping",
                            size="sm",
                        ).style(f"color: {COLORS['green']}")
                        ui.label(supplier_name).classes(
                            "text-subtitle1 text-weight-bold"
                        ).style(f"color: {COLORS['ink']}")
                        if has_deficit:
                            ui.badge("Déficit", color="red").props("outline")

                    # Table
                    columns = [
                        {"name": "label", "label": "Composant", "field": "label", "align": "left", "sortable": True},
                        {"name": "stock", "label": "Stock actuel", "field": "stock", "align": "right", "sortable": True},
                        {"name": "needed", "label": "Besoin planifié", "field": "needed", "align": "right", "sortable": True},
                        {"name": "after", "label": "Stock restant", "field": "after", "align": "right", "sortable": True},
                    ]
                    rows = []
                    for n in sorted(supplier_needs, key=lambda x: x.stock_after):
                        rows.append({
                            "label": n.label,
                            "stock": _fmt_qty(n.current_stock, n.unit),
                            "needed": _fmt_qty(n.total_needed, n.unit),
                            "after": _fmt_qty(n.stock_after, n.unit),
                            "_deficit": n.stock_after < 0,
                        })

                    table = ui.table(
                        columns=columns,
                        rows=rows,
                        row_key="label",
                    ).classes("w-full").props("flat dense")

                    # Color deficit rows
                    table.add_slot(
                        "body-cell-after",
                        r"""
                        <q-td :props="props">
                            <q-badge
                                :color="props.row._deficit ? 'red' : 'green'"
                                :label="props.value"
                                outline
                            />
                        </q-td>
                        """,
                    )
