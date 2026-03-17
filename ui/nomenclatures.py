"""
ui/nomenclatures.py
===================
Page Nomenclatures — Gestion du BOM (packaging par produit-format).

Permet de :
- Synchroniser les produits depuis EasyBeer (auto-détection)
- Visualiser et éditer le BOM pour chaque produit-format
- Valider les entrées détectées automatiquement
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from nicegui import ui

from common.product_bom import (
    delete_bom_entry,
    get_all_bom,
    upsert_bom_entry,
    validate_bom,
)
from ui.auth import require_auth
from ui.theme import COLORS, page_layout, section_title

_log = logging.getLogger("ferment.nomenclatures")


# ─── Helpers ────────────────────────────────────────────────────────────────

def _load_easybeer_mp() -> list[dict[str, Any]]:
    """Load all matières premières from EasyBeer."""
    try:
        from common.easybeer.stocks import get_all_matieres_premieres
        return get_all_matieres_premieres()
    except Exception:
        _log.warning("Impossible de charger les MP EasyBeer", exc_info=True)
        return []


def _load_product_formats() -> list[dict[str, Any]]:
    """Load product formats from barcode matrix."""
    try:
        from common.bom_detection import detect_product_formats
        from common.easybeer.conditioning import get_code_barre_matrice
        from common.easybeer.products import get_all_products

        barcode_matrix = get_code_barre_matrice()
        products = get_all_products()
        return detect_product_formats(barcode_matrix, products)
    except Exception:
        _log.warning("Impossible de charger les formats produits", exc_info=True)
        return []


def _bom_status(
    bom_entries: list[dict],
) -> tuple[str, str, str]:
    """Return (status_label, badge_color, icon) for a product-format BOM.

    Returns one of:
    - ("Complet", "green-7", "check_circle")   — all entries validated
    - ("À valider", "amber-8", "pending")       — has entries, not all validated
    - ("Vide", "red-6", "error")                — no entries
    """
    if not bom_entries:
        return "Vide", "red-6", "error"
    all_valid = all(e.get("validated", False) for e in bom_entries)
    if all_valid:
        return "Complet", "green-7", "check_circle"
    return "À valider", "amber-8", "pending"


def _mp_options(all_mp: list[dict]) -> dict[int, str]:
    """Build {id_mp: label} for dropdown options."""
    opts: dict[int, str] = {}
    for mp in all_mp:
        if not mp.get("actif", True):
            continue
        mp_id = mp.get("idMatierePremiere")
        label = (mp.get("libelle") or "").strip()
        if mp_id and label:
            opts[mp_id] = label
    return dict(sorted(opts.items(), key=lambda x: x[1]))


# ─── Page ───────────────────────────────────────────────────────────────────

@ui.page("/nomenclatures")
def page_nomenclatures():
    user = require_auth()
    if not user:
        return

    with page_layout("Nomenclatures", "account_tree", "/nomenclatures"):
        # ── Description ──
        ui.label(
            "Nomenclature des composants packaging par produit. "
            "Chaque produit-format est décomposé en étiquettes, bouteilles, "
            "capsules et cartons avec la quantité par carton vendu."
        ).classes("text-body2 q-mb-lg").style(f"color: {COLORS['ink2']}")

        # ── Action bar ──
        with ui.row().classes("items-center gap-3 q-mb-lg"):
            sync_btn = ui.button(
                "Synchroniser depuis EasyBeer",
                icon="sync",
            ).props("color=green-8 unelevated no-caps")

            sync_status = ui.label("").classes("text-caption").style(
                f"color: {COLORS['ink2']}"
            )

        # ── Main container (rebuilt on sync) ──
        main_container = ui.column().classes("w-full gap-4")

        # ── State ──
        state: dict[str, Any] = {
            "all_mp": [],
            "mp_options": {},
            "bom_entries": [],
            "product_formats": [],
        }

        def _rebuild_ui():
            """Rebuild the entire product list from current state."""
            main_container.clear()

            bom_entries = state["bom_entries"]
            product_formats = state["product_formats"]
            mp_options = state["mp_options"]

            # Group BOM entries by (id_produit, format_code)
            bom_by_key: dict[tuple[int, str], list[dict]] = {}
            for e in bom_entries:
                key = (e["id_produit"], e["format_code"])
                bom_by_key.setdefault(key, []).append(e)

            if not product_formats:
                with main_container:
                    ui.label(
                        "Aucun produit détecté. Cliquez sur « Synchroniser » "
                        "pour charger les produits depuis EasyBeer."
                    ).classes("text-body1 q-pa-lg").style(
                        f"color: {COLORS['ink2']}"
                    )
                return

            with main_container:
                for pf in product_formats:
                    _build_product_section(
                        pf, bom_by_key, mp_options, state["all_mp"],
                    )

        def _build_product_section(
            pf: dict,
            bom_by_key: dict[tuple[int, str], list[dict]],
            mp_options: dict[int, str],
            all_mp: list[dict],
        ):
            """Build one expansion panel per product with format sub-sections."""
            pid = pf["id_produit"]
            label = pf["libelle"]
            formats = pf["formats"]

            # Aggregate status across all formats
            all_format_entries = []
            for fmt in formats:
                key = (pid, fmt["format_code"])
                all_format_entries.extend(bom_by_key.get(key, []))

            status_label, badge_color, status_icon = _bom_status(all_format_entries)

            with ui.expansion(
                text=label,
                icon="inventory_2",
            ).classes("w-full").props("dense header-class=text-subtitle1"):
                # Badge in header
                with ui.element("template").props('v-slot:header'):
                    with ui.row().classes("w-full items-center no-wrap"):
                        ui.icon("inventory_2", size="sm").style(
                            f"color: {COLORS['green']}"
                        )
                        ui.label(label).classes(
                            "text-subtitle1 q-ml-sm"
                        ).style(f"color: {COLORS['ink']}; font-weight: 600")
                        ui.element("div").style("flex-grow: 1")
                        ui.badge(status_label).props(
                            f"color={badge_color}"
                        ).style("font-size: 11px")

                for fmt in formats:
                    _build_format_section(
                        pid, label, fmt, bom_by_key, mp_options, all_mp,
                    )

        def _build_format_section(
            id_produit: int,
            product_label: str,
            fmt: dict,
            bom_by_key: dict[tuple[int, str], list[dict]],
            mp_options: dict[int, str],
            all_mp: list[dict],
        ):
            """Build editable BOM table for one product-format."""
            format_code = fmt["format_code"]
            lot_qty = fmt["lot_qty"]
            key = (id_produit, format_code)
            entries = list(bom_by_key.get(key, []))

            status_label, badge_color, _ = _bom_status(entries)

            with ui.card().classes("w-full q-mb-sm").props("flat bordered").style(
                f"border: 1px solid {COLORS['border']}; border-radius: 6px"
            ):
                with ui.card_section().classes("q-pa-md"):
                    # Format header
                    with ui.row().classes("items-center gap-2 q-mb-sm"):
                        ui.label(f"Format {format_code}").classes(
                            "text-subtitle2"
                        ).style(f"color: {COLORS['ink']}; font-weight: 600")
                        ui.label(f"({lot_qty} unités/carton)").classes(
                            "text-caption"
                        ).style(f"color: {COLORS['ink2']}")
                        ui.element("div").style("flex-grow: 1")
                        ui.badge(status_label).props(
                            f"color={badge_color}"
                        ).style("font-size: 10px")

                    # BOM entries table
                    entries_container = ui.column().classes("w-full gap-2")

                    def _render_entries():
                        entries_container.clear()
                        with entries_container:
                            for entry in entries:
                                _render_entry_row(
                                    entry, id_produit, format_code,
                                    product_label, mp_options, entries,
                                )

                            # Add component button
                            with ui.row().classes("items-center gap-2 q-mt-sm"):
                                new_mp_select = ui.select(
                                    options=mp_options,
                                    label="Ajouter un composant",
                                    with_input=True,
                                ).props("outlined dense").classes("col")

                                new_qty_input = ui.number(
                                    label="Qté/carton",
                                    value=lot_qty,
                                    min=0,
                                ).props("outlined dense").style("width: 120px")

                                async def _add_entry(
                                    _sel=new_mp_select,
                                    _qty=new_qty_input,
                                ):
                                    mp_id = _sel.value
                                    qty = float(_qty.value or 0)
                                    if not mp_id or qty <= 0:
                                        ui.notify(
                                            "Sélectionnez un composant et une quantité",
                                            type="warning",
                                        )
                                        return
                                    mp_label = mp_options.get(mp_id, "")
                                    try:
                                        upsert_bom_entry(
                                            id_produit=id_produit,
                                            format_code=format_code,
                                            id_mp=mp_id,
                                            qty_per_unit=qty,
                                            product_label=product_label,
                                            mp_label=mp_label,
                                            validated=False,
                                            source="manual",
                                        )
                                        entries.append({
                                            "id_mp": mp_id,
                                            "mp_label": mp_label,
                                            "qty_per_unit": qty,
                                            "validated": False,
                                            "source": "manual",
                                        })
                                        _sel.value = None
                                        _render_entries()
                                        ui.notify(f"{mp_label} ajouté", type="positive")
                                    except Exception as exc:
                                        _log.exception("Erreur ajout BOM")
                                        ui.notify(f"Erreur : {exc}", type="negative")

                                ui.button(
                                    icon="add", on_click=_add_entry,
                                ).props(
                                    "round flat color=green-8 size=sm"
                                )

                    _render_entries()

                    # Validate button
                    with ui.row().classes("w-full justify-end q-mt-sm"):
                        async def _validate(
                            _pid=id_produit, _fc=format_code, _entries=entries,
                        ):
                            try:
                                validate_bom(_pid, _fc)
                                for e in _entries:
                                    e["validated"] = True
                                _render_entries()
                                ui.notify(
                                    f"{product_label} {format_code} validé",
                                    type="positive",
                                )
                            except Exception as exc:
                                ui.notify(f"Erreur : {exc}", type="negative")

                        ui.button(
                            "Valider ce format",
                            icon="check",
                            on_click=_validate,
                        ).props("flat no-caps color=green-8")

        def _render_entry_row(
            entry: dict,
            id_produit: int,
            format_code: str,
            product_label: str,
            mp_options: dict[int, str],
            entries_list: list[dict],
        ):
            """Render one editable BOM entry row."""
            mp_id = entry["id_mp"]
            mp_label = entry.get("mp_label", mp_options.get(mp_id, "?"))
            qty = entry.get("qty_per_unit", 0)
            is_validated = entry.get("validated", False)
            source = entry.get("source", "manual")

            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                # Component select
                ui.select(
                    options=mp_options,
                    value=mp_id,
                    with_input=True,
                    on_change=lambda e, _entry=entry, _pid=id_produit,
                    _fc=format_code, _old_mp=mp_id, _pl=product_label: _change_mp(
                        e, _entry, _pid, _fc, _old_mp, _pl, mp_options,
                    ),
                ).props("outlined dense").classes("col")

                # Quantity input
                ui.number(
                    value=qty,
                    min=0,
                    on_change=lambda e, _entry=entry, _pid=id_produit,
                    _fc=format_code, _mp=mp_id, _pl=product_label: _change_qty(
                        e, _entry, _pid, _fc, _mp, _pl, mp_options,
                    ),
                ).props("outlined dense").style("width: 100px")

                # Source badge
                if source == "conditioning":
                    ui.badge("prod").props("color=green-7").style("font-size: 9px")
                elif source == "auto_detected":
                    ui.badge("auto").props("color=amber-8").style("font-size: 9px")

                # Validated icon
                if is_validated:
                    ui.icon("check_circle", size="xs").style(
                        f"color: {COLORS['success']}"
                    )

                # Delete button
                async def _delete(
                    _pid=id_produit, _fc=format_code,
                    _mp=mp_id, _entry=entry, _list=entries_list,
                ):
                    try:
                        delete_bom_entry(_pid, _fc, _mp)
                        _list.remove(_entry)
                        ui.notify("Composant supprimé", type="info")
                    except Exception as exc:
                        ui.notify(f"Erreur : {exc}", type="negative")

                ui.button(
                    icon="delete", on_click=_delete,
                ).props("round flat color=red-6 size=xs")

        def _change_mp(e, entry, pid, fc, old_mp, pl, opts):
            """Handle component change in dropdown."""
            new_mp = e.value
            if new_mp == old_mp or not new_mp:
                return
            try:
                delete_bom_entry(pid, fc, old_mp)
                new_label = opts.get(new_mp, "")
                upsert_bom_entry(
                    id_produit=pid, format_code=fc, id_mp=new_mp,
                    qty_per_unit=entry.get("qty_per_unit", 0),
                    product_label=pl, mp_label=new_label,
                    source="manual",
                )
                entry["id_mp"] = new_mp
                entry["mp_label"] = new_label
                entry["source"] = "manual"
                ui.notify(f"Composant changé → {new_label}", type="positive")
            except Exception as exc:
                ui.notify(f"Erreur : {exc}", type="negative")

        def _change_qty(e, entry, pid, fc, mp_id, pl, opts):
            """Handle quantity change."""
            new_qty = float(e.value or 0)
            if new_qty == entry.get("qty_per_unit", 0):
                return
            try:
                upsert_bom_entry(
                    id_produit=pid, format_code=fc, id_mp=mp_id,
                    qty_per_unit=new_qty,
                    product_label=pl, mp_label=opts.get(mp_id, ""),
                    validated=entry.get("validated", False),
                    source="manual",
                )
                entry["qty_per_unit"] = new_qty
            except Exception as exc:
                ui.notify(f"Erreur : {exc}", type="negative")

        # ── Sync button handler ──
        async def _do_sync():
            sync_btn.disable()
            sync_status.text = "Chargement depuis EasyBeer..."

            try:
                # Load data in background threads
                all_mp, product_formats = await asyncio.gather(
                    asyncio.to_thread(_load_easybeer_mp),
                    asyncio.to_thread(_load_product_formats),
                )

                state["all_mp"] = all_mp
                state["mp_options"] = _mp_options(all_mp)
                state["product_formats"] = product_formats

                # Run auto-detection
                sync_status.text = "Auto-détection des composants..."
                from common.bom_detection import run_full_detection
                total, nb_products = await asyncio.to_thread(run_full_detection)

                # Reload BOM from DB
                state["bom_entries"] = await asyncio.to_thread(get_all_bom)

                _rebuild_ui()

                sync_status.text = (
                    f"{nb_products} produits, {total} composants détectés"
                )
                ui.notify(
                    f"Synchronisation terminée : {total} composants "
                    f"pour {nb_products} produits",
                    type="positive",
                )
            except Exception as exc:
                _log.exception("Erreur synchronisation BOM")
                sync_status.text = f"Erreur : {exc}"
                ui.notify(f"Erreur : {exc}", type="negative")
            finally:
                sync_btn.enable()

        sync_btn.on_click(_do_sync)

        # ── Initial load (from DB only, no EasyBeer calls) ──
        async def _initial_load():
            try:
                all_mp, product_formats, bom_entries = await asyncio.gather(
                    asyncio.to_thread(_load_easybeer_mp),
                    asyncio.to_thread(_load_product_formats),
                    asyncio.to_thread(get_all_bom),
                )
                state["all_mp"] = all_mp
                state["mp_options"] = _mp_options(all_mp)
                state["product_formats"] = product_formats
                state["bom_entries"] = bom_entries
                _rebuild_ui()
            except Exception:
                _log.exception("Erreur chargement initial nomenclatures")
                with main_container:
                    ui.label(
                        "Erreur de chargement. Cliquez sur Synchroniser."
                    ).classes("text-body1").style(f"color: {COLORS['error']}")

        ui.timer(0.1, _initial_load, once=True)
