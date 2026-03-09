"""
ui/ressources.py
================
Page Ressources — Contraintes de commande fournisseurs editables.

Affiche une carte par fournisseur (depuis config.yaml + surcharges DB),
groupees par categorie. Chaque carte permet de modifier les contraintes
de commande (delai, palettes min, bouteilles/palette, notes) et de
sauvegarder en DB.
"""
from __future__ import annotations

import logging
from typing import Any

from nicegui import ui

from common.supplier_config import (
    get_all_suppliers_with_config,
    upsert_supplier_config,
)
from ui.auth import require_auth
from ui.theme import COLORS, page_layout, section_title

_log = logging.getLogger("ferment.ressources")


# ─── Supplier card builder ──────────────────────────────────────────────────

def _build_supplier_card(supplier: dict[str, Any]) -> None:
    """Build an editable card for one supplier."""
    name = supplier["name"]
    icon = supplier.get("icon", "business")
    ordering = supplier.get("ordering") or {}

    # Current values (from merged config)
    lead_time = ordering.get("lead_time_days")
    min_pallets = ordering.get("min_order_pallets")
    can_split = ordering.get("can_split_references", False)
    pallets_cfg = ordering.get("pallets") or {}
    notes = ordering.get("notes", "")

    # ── State holders for form inputs ──
    inputs: dict[str, Any] = {}

    with ui.card().classes("w-full").props("flat bordered").style(
        f"border: 1px solid {COLORS['border']}; border-radius: 8px"
    ):
        # ── Header ──
        with ui.card_section().classes("row items-center justify-between q-pa-md"):
            with ui.row().classes("items-center gap-2"):
                ui.icon(icon, size="sm").style(f"color: {COLORS['green']}")
                ui.label(name).classes("text-subtitle1").style(
                    f"color: {COLORS['ink']}; font-weight: 600"
                )
            save_btn = ui.button(
                "Sauvegarder", icon="save",
            ).props("unelevated color=green-8 dense").classes("q-px-md")

        ui.separator()

        # ── Form body ──
        with ui.card_section().classes("q-pa-md"):
            with ui.row().classes("w-full gap-4 items-start").style("flex-wrap: wrap"):
                # Left column: numeric fields
                with ui.column().classes("gap-3").style("min-width: 180px; flex: 1"):
                    inputs["lead_time"] = ui.number(
                        label="Delai livraison (jours)",
                        value=lead_time,
                        min=0, max=365, step=1,
                    ).props("outlined dense").classes("w-full")

                    inputs["min_pallets"] = ui.number(
                        label="Commande min. (palettes)",
                        value=min_pallets,
                        min=0, max=999, step=1,
                    ).props("outlined dense").classes("w-full")

                    inputs["can_split"] = ui.checkbox(
                        "Repartition libre entre references",
                        value=can_split,
                    ).style(f"color: {COLORS['ink']}")

                # Right column: pallets per reference
                with ui.column().classes("gap-3").style("min-width: 180px; flex: 1"):
                    if pallets_cfg:
                        ui.label("References palette").classes("text-caption").style(
                            f"color: {COLORS['ink2']}; font-weight: 600"
                        )
                        pallet_inputs: dict[str, ui.number] = {}
                        for ref_name, ref_cfg in pallets_cfg.items():
                            bpp = ref_cfg.get("bottles_per_pallet")
                            with ui.row().classes("items-center gap-2 w-full"):
                                ui.label(ref_name).classes("text-body2").style(
                                    f"color: {COLORS['ink']}; flex: 1; min-width: 140px"
                                )
                                inp = ui.number(
                                    value=bpp, min=1, max=99999, step=1,
                                ).props("outlined dense suffix=/pal").style("width: 130px")
                                pallet_inputs[ref_name] = inp
                        inputs["pallets"] = pallet_inputs
                    else:
                        ui.label("Aucune reference palette configuree").classes(
                            "text-body2"
                        ).style(f"color: {COLORS['ink2']}")
                        inputs["pallets"] = {}

            # Notes textarea (full width)
            ui.separator().classes("q-my-sm")
            inputs["notes"] = ui.textarea(
                label="Notes (references, contacts, conditions...)",
                value=notes,
            ).props("outlined dense autogrow").classes("w-full")

        # ── Save handler ──
        async def _save(
            _e=None,
            _name=name,
            _inputs=inputs,
        ):
            config: dict[str, Any] = {}

            # Lead time
            val = _inputs["lead_time"].value
            if val is not None and val != "":
                config["lead_time_days"] = int(val)

            # Min pallets
            val = _inputs["min_pallets"].value
            if val is not None and val != "":
                config["min_order_pallets"] = int(val)

            # Can split
            config["can_split_references"] = _inputs["can_split"].value

            # Pallets per reference
            pallet_dict = _inputs.get("pallets") or {}
            if pallet_dict:
                pallets: dict[str, dict] = {}
                for ref, inp in pallet_dict.items():
                    if inp.value is not None and inp.value != "":
                        pallets[ref] = {"bottles_per_pallet": int(inp.value)}
                if pallets:
                    config["pallets"] = pallets

            # Notes
            notes_val = (_inputs["notes"].value or "").strip()
            if notes_val:
                config["notes"] = notes_val

            try:
                upsert_supplier_config(_name, config)
                ui.notify(
                    f"{_name} — configuration sauvegardee",
                    type="positive",
                )
            except Exception as exc:
                _log.exception("Erreur sauvegarde config %s", _name)
                ui.notify(
                    f"Erreur : {exc}",
                    type="negative",
                )

        save_btn.on_click(_save)


# ─── Page ───────────────────────────────────────────────────────────────────

@ui.page("/ressources")
def page_ressources():
    user = require_auth()
    if not user:
        return

    with page_layout("Ressources", "menu_book", "/ressources"):
        ui.label(
            "Contraintes de commande par fournisseur. "
            "Les modifications sont sauvegardees en base de donnees "
            "et utilisees dans l'analyse des stocks."
        ).classes("text-body2").style(f"color: {COLORS['ink2']}")

        # Load all suppliers with merged config
        try:
            suppliers = get_all_suppliers_with_config()
        except Exception as exc:
            _log.exception("Erreur chargement config fournisseurs")
            ui.label(f"Erreur : {exc}").style(f"color: {COLORS['error']}")
            return

        # Group by category
        categories: dict[str, list[dict]] = {}
        for s in suppliers:
            cat = s.get("category", "Autre")
            categories.setdefault(cat, []).append(s)

        # Render cards grouped by category (3-column grid)
        for cat_name, cat_suppliers in categories.items():
            section_title(cat_name, "category")

            with ui.element("div").style(
                "display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px;"
            ):
                for supplier in cat_suppliers:
                    _build_supplier_card(supplier)
