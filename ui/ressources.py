"""
ui/ressources.py
================
Page Ressources — Contraintes de commande fournisseurs editables.

Affiche une carte par fournisseur (depuis config.yaml + surcharges DB),
groupees par categorie. Les references sont auto-decouvertes depuis
l'API EasyBeer et matchees par ID stable (idMatierePremiere).
"""
from __future__ import annotations

import logging
from typing import Any

from nicegui import ui

from common.supplier_config import (
    discover_supplier_refs,
    get_all_suppliers_with_config,
    match_ref_config,
    upsert_supplier_config,
)
from ui.auth import require_auth
from ui.theme import COLORS, page_layout, section_title

_log = logging.getLogger("ferment.ressources")

_ORDER_UNIT_OPTIONS = ["palette", "carton", "bidon", "lot"]
_QTY_UNIT_OPTIONS = ["unités", "kg", "capsules"]


# ─── EasyBeer MP loader (cached at page level) ──────────────────────────────

def _load_easybeer_mp() -> list[dict[str, Any]]:
    """Load all MP from EasyBeer. Returns [] on error."""
    try:
        from common.easybeer.stocks import get_all_matieres_premieres
        return get_all_matieres_premieres()
    except Exception:
        _log.warning("Impossible de charger les MP EasyBeer", exc_info=True)
        return []


# ─── Supplier card builder ──────────────────────────────────────────────────

def _build_supplier_card(
    supplier: dict[str, Any],
    live_refs: list[dict[str, Any]],
) -> None:
    """Build an editable card for one supplier.

    live_refs: output of match_ref_config() — [{eb_id, label, qty_per_unit, min_qty, is_new}, ...]
    """
    name = supplier["name"]
    icon = supplier.get("icon", "business")
    is_active = supplier.get("active", True)
    ordering = supplier.get("ordering") or {}

    # Current values (from merged config)
    lead_time = ordering.get("lead_time_days")
    min_order = ordering.get("min_order")
    can_split = ordering.get("can_split_references", False)
    order_unit = ordering.get("order_unit", "palette")
    qty_unit = ordering.get("qty_unit", "unités")
    notes = ordering.get("notes", "")

    # ── State holders for form inputs ──
    inputs: dict[str, Any] = {}

    card = ui.card().classes("w-full").props("flat bordered").style(
        f"border: 1px solid {COLORS['border']}; border-radius: 8px"
    )
    if not is_active:
        card.style(add="opacity: 0.5")

    with card:
        # ── Header ──
        with ui.card_section().classes("q-pa-md").style(
            "display: flex; align-items: center; justify-content: space-between; gap: 8px"
        ):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon(icon, size="sm").style(f"color: {COLORS['green']}")
                ui.label(name).classes("text-subtitle1").style(
                    f"color: {COLORS['ink']}; font-weight: 600"
                )

            with ui.row().classes("items-center gap-2 no-wrap"):
                # Active toggle
                inputs["active"] = ui.switch(
                    "Actif", value=is_active,
                ).props("dense color=green-8")

                def _on_toggle(e, _card=card):
                    if e.value:
                        _card.style(remove="opacity: 0.5")
                    else:
                        _card.style(add="opacity: 0.5")

                inputs["active"].on_value_change(_on_toggle)

                save_btn = ui.button(
                    "Sauvegarder", icon="save",
                ).props("unelevated color=green-8 dense no-wrap").classes("q-px-md")

        ui.separator()

        # ── Form body ──
        with ui.card_section().classes("q-pa-md"):
            with ui.row().classes("w-full gap-4 items-start").style("flex-wrap: wrap"):
                # Left column: numeric fields + unit selectors
                with ui.column().classes("gap-3").style("min-width: 200px; flex: 1"):
                    inputs["lead_time"] = ui.number(
                        label="Délai livraison (jours)",
                        value=lead_time,
                        min=0, max=365, step=1,
                    ).props("outlined dense").classes("w-full")

                    # Order unit selector
                    inputs["order_unit"] = ui.select(
                        label="Unité de commande",
                        options=_ORDER_UNIT_OPTIONS,
                        value=order_unit,
                    ).props("outlined dense").classes("w-full")

                    inputs["min_order"] = ui.number(
                        label=f"Commande minimum ({order_unit}s)",
                        value=min_order,
                        min=0, max=9999, step=1,
                    ).props("outlined dense").classes("w-full")

                    # Update min_order label when order_unit changes
                    def _update_min_label(e, _inp=inputs):
                        _inp["min_order"].props(
                            f'label="Commande minimum ({e.value}s)"'
                        )
                    inputs["order_unit"].on_value_change(_update_min_label)

                    # Qty unit selector
                    inputs["qty_unit"] = ui.select(
                        label="Unité de quantité",
                        options=_QTY_UNIT_OPTIONS,
                        value=qty_unit,
                    ).props("outlined dense").classes("w-full")

                    inputs["can_split"] = ui.checkbox(
                        "Répartition libre entre références",
                        value=can_split,
                    ).style(f"color: {COLORS['ink']}")

                # Right column: references (from EasyBeer auto-discovery)
                with ui.column().classes("gap-3").style("min-width: 200px; flex: 1"):
                    if live_refs:
                        with ui.row().classes("items-center gap-2"):
                            ui.label("Références").classes("text-caption").style(
                                f"color: {COLORS['ink2']}; font-weight: 600"
                            )
                            ui.badge("EasyBeer", color="green-8").props(
                                "outline"
                            ).style("font-size: 10px")

                        ref_inputs: list[dict[str, Any]] = []
                        for ref in live_refs:
                            with ui.column().classes("w-full gap-1"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label(ref["label"]).classes(
                                        "text-body2"
                                    ).style(
                                        f"color: {COLORS['ink']}; font-weight: 500"
                                    )
                                    if ref.get("is_new"):
                                        ui.badge(
                                            "nouveau", color="orange-8"
                                        ).props("outline").style("font-size: 9px")
                                with ui.row().classes("items-center gap-2 w-full"):
                                    qpu_inp = ui.number(
                                        label="Qté/unité",
                                        value=ref["qty_per_unit"] or None,
                                        min=0, max=999999, step=1,
                                    ).props("outlined dense").style("flex: 1")
                                    mq_inp = ui.number(
                                        label="Min. qté",
                                        value=ref.get("min_qty"),
                                        min=0, max=9999999, step=1,
                                    ).props("outlined dense").style("flex: 1")
                                    ref_inputs.append({
                                        "eb_id": ref["eb_id"],
                                        "label": ref["label"],
                                        "qty_per_unit": qpu_inp,
                                        "min_qty": mq_inp,
                                    })
                        inputs["references"] = ref_inputs
                    else:
                        # Fallback: show config-based refs if no EasyBeer data
                        refs_cfg = ordering.get("references") or {}
                        if refs_cfg:
                            ui.label("Références").classes("text-caption").style(
                                f"color: {COLORS['ink2']}; font-weight: 600"
                            )
                            ref_inputs_legacy: list[dict[str, Any]] = []
                            for ref_name, ref_data in refs_cfg.items():
                                qpu = ref_data.get("qty_per_unit")
                                min_qty = ref_data.get("min_qty")
                                eb_id = ref_data.get("eb_id")
                                with ui.column().classes("w-full gap-1"):
                                    ui.label(ref_name).classes("text-body2").style(
                                        f"color: {COLORS['ink']}; font-weight: 500"
                                    )
                                    with ui.row().classes(
                                        "items-center gap-2 w-full"
                                    ):
                                        qpu_inp = ui.number(
                                            label="Qté/unité",
                                            value=qpu, min=0, max=999999, step=1,
                                        ).props("outlined dense").style("flex: 1")
                                        mq_inp = ui.number(
                                            label="Min. qté",
                                            value=min_qty, min=0, max=9999999,
                                            step=1,
                                        ).props("outlined dense").style("flex: 1")
                                        ref_inputs_legacy.append({
                                            "eb_id": eb_id,
                                            "label": ref_name,
                                            "qty_per_unit": qpu_inp,
                                            "min_qty": mq_inp,
                                        })
                            inputs["references"] = ref_inputs_legacy
                        else:
                            ui.label("Aucune référence configurée").classes(
                                "text-body2"
                            ).style(f"color: {COLORS['ink2']}")
                            inputs["references"] = []

            # Notes textarea (full width)
            ui.separator().classes("q-my-sm")
            inputs["notes"] = ui.textarea(
                label="Notes (références, contacts, conditions...)",
                value=notes,
            ).props("outlined dense autogrow").classes("w-full")

        # ── Save handler ──
        async def _save(
            _e=None,
            _name=name,
            _inputs=inputs,
        ):
            config: dict[str, Any] = {}

            # Active flag
            config["active"] = _inputs["active"].value

            # Lead time
            val = _inputs["lead_time"].value
            if val is not None and val != "":
                config["lead_time_days"] = int(val)

            # Order unit
            ou_val = _inputs["order_unit"].value
            if ou_val:
                config["order_unit"] = ou_val

            # Min order
            val = _inputs["min_order"].value
            if val is not None and val != "":
                config["min_order"] = int(val)

            # Qty unit
            qu_val = _inputs["qty_unit"].value
            if qu_val:
                config["qty_unit"] = qu_val

            # Can split
            config["can_split_references"] = _inputs["can_split"].value

            # References with eb_id (auto-synced from EasyBeer)
            ref_list = _inputs.get("references") or []
            if ref_list:
                references: dict[str, dict] = {}
                for ref_inp in ref_list:
                    label = ref_inp["label"]
                    ref_entry: dict[str, Any] = {}
                    # Store eb_id for ID-based matching
                    if ref_inp.get("eb_id"):
                        ref_entry["eb_id"] = ref_inp["eb_id"]
                    qpu_val = ref_inp["qty_per_unit"].value
                    if qpu_val is not None and qpu_val != "":
                        ref_entry["qty_per_unit"] = int(qpu_val)
                    mq_val = ref_inp["min_qty"].value
                    if mq_val is not None and mq_val != "" and int(mq_val) > 0:
                        ref_entry["min_qty"] = int(mq_val)
                    if ref_entry:
                        references[label] = ref_entry
                if references:
                    config["references"] = references

            # Notes
            notes_val = (_inputs["notes"].value or "").strip()
            if notes_val:
                config["notes"] = notes_val

            try:
                upsert_supplier_config(_name, config)
                ui.notify(
                    f"{_name} — configuration sauvegardée",
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
            "Les références sont synchronisées depuis EasyBeer. "
            "Les modifications sont sauvegardées en base de données."
        ).classes("text-body2").style(f"color: {COLORS['ink2']}")

        # Load all suppliers with merged config
        try:
            suppliers = get_all_suppliers_with_config()
        except Exception as exc:
            _log.exception("Erreur chargement config fournisseurs")
            ui.label(f"Erreur : {exc}").style(f"color: {COLORS['error']}")
            return

        # Load EasyBeer MP for auto-discovery
        all_mp = _load_easybeer_mp()

        # Group by category
        categories: dict[str, list[dict]] = {}
        for s in suppliers:
            cat = s.get("category", "Autre")
            categories.setdefault(cat, []).append(s)

        # Render cards grouped by category (2-column grid)
        for cat_name, cat_suppliers in categories.items():
            section_title(cat_name, "category")

            with ui.element("div").style(
                "display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px;"
            ):
                for supplier in cat_suppliers:
                    # Auto-discover refs from EasyBeer
                    ordering_refs = (supplier.get("ordering") or {}).get(
                        "references", {}
                    )
                    if all_mp:
                        discovered = discover_supplier_refs(supplier, all_mp)
                        live_refs = match_ref_config(discovered, ordering_refs)
                    else:
                        live_refs = []

                    _build_supplier_card(supplier, live_refs)
