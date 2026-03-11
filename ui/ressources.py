"""
ui/ressources.py
================
Page Ressources — Instructions de commande fournisseurs (prompts IA).

Chaque fournisseur a :
- Un délai de livraison (structuré, utilisé pour le calcul d'urgence)
- Des références auto-découvertes depuis EasyBeer (lecture seule)
- Des instructions en langage naturel que l'IA utilisera pour proposer des commandes
"""
from __future__ import annotations

import logging
from typing import Any

from nicegui import ui

from common.supplier_config import (
    discover_supplier_refs,
    generate_instructions_from_config,
    get_all_suppliers_with_config,
    match_ref_config,
    upsert_supplier_config,
)
from ui.auth import require_auth
from ui.theme import COLORS, page_layout, section_title

_log = logging.getLogger("ferment.ressources")


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

    live_refs: output of match_ref_config() — [{eb_id, label, unit, ...}, ...]
    """
    name = supplier["name"]
    icon = supplier.get("icon", "business")
    is_active = supplier.get("active", True)
    ordering = supplier.get("ordering") or {}

    # Current values
    lead_time = ordering.get("lead_time_days")

    # AI instructions: from DB override, or generate from structured config
    ai_instructions = ordering.get("ai_instructions", "")
    if not ai_instructions:
        ai_instructions = generate_instructions_from_config(ordering)

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
            # Lead time (structured — used for urgency calculation)
            inputs["lead_time"] = ui.number(
                label="Délai livraison (jours)",
                value=lead_time,
                min=0, max=365, step=1,
            ).props("outlined dense").style("max-width: 200px")

            # References (read-only, from EasyBeer auto-discovery)
            if live_refs:
                with ui.row().classes("items-center gap-2 q-mt-md"):
                    ui.label("Références").classes("text-caption").style(
                        f"color: {COLORS['ink2']}; font-weight: 600"
                    )
                    ui.badge("EasyBeer", color="green-8").props(
                        "outline"
                    ).style("font-size: 10px")

                with ui.element("div").classes("q-mt-xs").style(
                    f"display: flex; flex-wrap: wrap; gap: 6px;"
                ):
                    for ref in live_refs:
                        with ui.element("span").style(
                            f"background: {COLORS['green']}0A; "
                            f"border: 1px solid {COLORS['green']}30; "
                            "border-radius: 4px; padding: 2px 8px; "
                            "font-size: 12px;"
                        ):
                            ui.label(ref["label"]).style(
                                f"color: {COLORS['ink']}"
                            )

                # Store ref data for save
                inputs["eb_refs"] = [
                    {"eb_id": r["eb_id"], "label": r["label"]}
                    for r in live_refs
                ]
            else:
                inputs["eb_refs"] = []

            # AI instructions (the core of the new approach)
            ui.separator().classes("q-my-md")
            with ui.row().classes("items-center gap-2"):
                ui.icon("smart_toy", size="xs").style(
                    f"color: {COLORS['green']}"
                )
                ui.label("Instructions de commande (IA)").classes(
                    "text-caption"
                ).style(
                    f"color: {COLORS['ink2']}; font-weight: 600"
                )

            inputs["ai_instructions"] = ui.textarea(
                value=ai_instructions,
                placeholder=(
                    "Décrivez les contraintes de commande pour ce fournisseur : "
                    "minimum de commande, conditionnement par référence, "
                    "conditions particulières..."
                ),
            ).props("outlined autogrow").classes("w-full q-mt-xs").style(
                "min-height: 120px; font-size: 13px"
            )

            ui.label(
                "Ces instructions seront utilisées par l'IA pour analyser "
                "vos stocks et proposer des commandes adaptées."
            ).classes("text-caption").style(
                f"color: {COLORS['ink2']}; font-style: italic"
            )

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

            # AI instructions
            instr = (_inputs["ai_instructions"].value or "").strip()
            if instr:
                config["ai_instructions"] = instr

            # EasyBeer refs (eb_id mapping for reference tracking)
            eb_refs = _inputs.get("eb_refs") or []
            if eb_refs:
                config["eb_refs"] = eb_refs

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
            "Instructions de commande par fournisseur. "
            "Les références sont synchronisées depuis EasyBeer. "
            "L'IA utilisera ces instructions pour analyser vos stocks "
            "et proposer des commandes adaptées."
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
