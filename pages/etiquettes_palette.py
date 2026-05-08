"""
pages/etiquettes_palette.py
============================
Page d'édition d'étiquettes palette logistique avec code-barres GS1-128.

Flux opérateur (iPad-friendly) — 3 sélecteurs cascadés :
  1. Marque : NIKO / SYMBIOSE
  2. Type de bouteille : 33cl / 75cl SAFT / 75cl Eau gazeuse
  3. Goût : Gingembre, Mangue Passion, Original, …

Les données proviennent de la dernière sync étiquettes (table ``sync_operations``,
même source que la page Paramètres → Étiquettes). Pas d'aller-retour EasyBeer
direct : si l'opérateur veut des données fraîches, il lance la sync depuis
Paramètres → Étiquettes.

Une fois le produit sélectionné, l'opérateur indique :
  - Soit "palette pleine"
  - Soit étages pleins + caisses sur le dernier étage incomplet
puis clique pour générer le PDF (102×152 mm, AirPrint vers Dymo 5XL Wireless).
"""
from __future__ import annotations

import asyncio
import logging

from nicegui import ui

from common.ramasse import get_palette_layout
from common.services.etiquette_palette_service import (
    BOTTLE_TYPES,
    LabelEntry,
    compute_case_count,
    load_label_data_from_sync,
)
from pages.auth import require_auth
from pages.theme import COLORS, error_banner, page_layout, section_title

_log = logging.getLogger("ferment.etiquettes_palette")


@ui.page("/etiquettes-palette")
async def page_etiquettes_palette():
    user = require_auth()
    if not user:
        return

    tenant_id = user.get("tenant_id", "")

    with page_layout("Étiquettes palette", "qr_code_2", "/etiquettes-palette"):
        ui.label(
            "Édite une étiquette logistique GS1-128 pour palette filmée — "
            "imprime via AirPrint depuis l'iPad."
        ).classes("text-body2").style(f"color: {COLORS['ink2']}")

        loading_card = ui.card().classes("w-full q-pa-lg items-center").props("flat bordered")
        with loading_card:
            ui.spinner("dots", size="lg", color="green-8")
            ui.label("Chargement des produits…").classes("text-body2 q-mt-sm")

        try:
            entries, info_msg = await asyncio.to_thread(load_label_data_from_sync, tenant_id)
        except Exception as exc:
            _log.exception("Erreur chargement payload sync étiquettes")
            loading_card.delete()
            error_banner(f"Impossible de charger les données : {exc}", dismissible=False)
            return

        loading_card.delete()

        if info_msg:
            error_banner(info_msg, dismissible=True)

        if not entries:
            return

        _render_form(entries, tenant_name=user.get("tenant_name") or _resolve_tenant_name())


# ─── UI principale ──────────────────────────────────────────────────────────

def _render_form(entries: list[LabelEntry], tenant_name: str = "") -> None:
    """Rend le formulaire avec 3 sélecteurs cascadés + section quantité."""
    state: dict = {
        "marque": None,        # str
        "bottle": None,        # str
        "gout": None,          # str
        "entry": None,         # LabelEntry sélectionnée (résolue via la cascade)
    }

    # ────────────────────────────────────────────────────────────────────
    # Step 1 — Marque
    # ────────────────────────────────────────────────────────────────────
    section_title("1. Marque", "branding_watermark")
    marques_dispo = sorted({e.marque for e in entries})
    marque_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
    marque_buttons: dict[str, ui.button] = {}
    with marque_card:
        with ui.row().classes("w-full gap-3"):
            for m in marques_dispo:
                btn = ui.button(m).classes("flex-1").props("size=lg outline color=green-8")
                marque_buttons[m] = btn

    # ────────────────────────────────────────────────────────────────────
    # Step 2 — Type de bouteille
    # ────────────────────────────────────────────────────────────────────
    section_title("2. Type de bouteille", "wine_bar")
    bottle_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
    bottle_buttons: dict[str, ui.button] = {}
    with bottle_card:
        with ui.row().classes("w-full gap-3"):
            for bt in BOTTLE_TYPES:
                btn = ui.button(bt).classes("flex-1").props("size=lg outline color=green-8")
                btn.disable()
                bottle_buttons[bt] = btn

    # ────────────────────────────────────────────────────────────────────
    # Step 3 — Goût
    # ────────────────────────────────────────────────────────────────────
    section_title("3. Goût", "local_drink")
    gout_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
    with gout_card:
        gout_select = ui.select(
            options=[],
            label="Choisir le goût",
            with_input=True,
        ).classes("w-full").props("outlined dense fill-input use-input input-debounce=0")
        gout_select.disable()

    # ────────────────────────────────────────────────────────────────────
    # Récapitulatif produit
    # ────────────────────────────────────────────────────────────────────
    recap_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
    with recap_card:
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("info", size="sm").style(f"color: {COLORS['blue']}")
            recap_label = ui.label(
                "Sélectionne marque, bouteille et goût pour voir les détails du produit."
            ).classes("text-body2").style(f"color: {COLORS['ink2']}")
        recap_details = ui.column().classes("w-full gap-1 q-mt-sm")
        recap_details.set_visibility(False)

    # ────────────────────────────────────────────────────────────────────
    # Step 4 — Quantité
    # ────────────────────────────────────────────────────────────────────
    section_title("4. Quantité de caisses", "inventory_2")
    qty_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
    with qty_card:
        full_pallet_toggle = ui.switch("Palette pleine", value=True).classes("q-mb-sm")

        partial_container = ui.column().classes("w-full gap-2")
        with partial_container:
            layers_label = ui.label("Étages pleins").classes("text-body2")
            layers_input = ui.number(value=0, min=0, max=7, step=1).classes("w-full").props(
                "outlined dense",
            )

            extras_label = ui.label("Caisses sur le dernier étage (incomplet)").classes(
                "text-body2 q-mt-xs",
            )
            extras_input = ui.number(value=0, min=0, max=35, step=1).classes("w-full").props(
                "outlined dense",
            )

        partial_container.set_visibility(False)

        ui.separator().classes("q-my-sm")
        total_display = ui.label("Total : —").classes("text-h6").style(
            f"color: {COLORS['green']}; font-weight: 600",
        )

    # ────────────────────────────────────────────────────────────────────
    # Step 5 — Action
    # ────────────────────────────────────────────────────────────────────
    section_title("5. Génération de l'étiquette", "qr_code_2")
    with ui.row().classes("w-full gap-3"):
        generate_btn = ui.button(
            "Générer & télécharger le PDF",
            icon="picture_as_pdf",
        ).classes("flex-1").props("color=green-8 unelevated size=lg")
        generate_btn.disable()

    # ────────────────────────────────────────────────────────────────────
    # Logique réactive
    # ────────────────────────────────────────────────────────────────────

    def _set_active_button(buttons: dict[str, ui.button], active_key: str | None):
        """Marque visuellement le bouton sélectionné (color=green-8 unelevated)."""
        for key, btn in buttons.items():
            if key == active_key:
                btn.props(remove="outline")
                btn.props("unelevated color=green-8")
            else:
                btn.props(remove="unelevated")
                btn.props("outline color=green-8")

    def _filter_entries() -> list[LabelEntry]:
        """Retourne les entries qui matchent l'état courant (marque, bottle, gout)."""
        out = entries
        if state["marque"]:
            out = [e for e in out if e.marque == state["marque"]]
        if state["bottle"]:
            out = [e for e in out if e.bottle_type == state["bottle"]]
        if state["gout"]:
            out = [e for e in out if e.gout == state["gout"]]
        return out

    def _refresh_bottles():
        """Active uniquement les bouteilles disponibles pour la marque choisie."""
        if not state["marque"]:
            for btn in bottle_buttons.values():
                btn.disable()
            return
        available = {
            e.bottle_type
            for e in entries
            if e.marque == state["marque"]
        }
        for bt, btn in bottle_buttons.items():
            if bt in available:
                btn.enable()
            else:
                btn.disable()

    def _refresh_gouts():
        """Met à jour les options du sélecteur goût."""
        if not (state["marque"] and state["bottle"]):
            gout_select.options = []
            gout_select.value = None
            gout_select.disable()
            return
        gouts = sorted({
            e.gout
            for e in entries
            if e.marque == state["marque"] and e.bottle_type == state["bottle"]
        }, key=str.lower)
        gout_select.options = gouts
        gout_select.value = None
        gout_select.enable()

    def _refresh_recap():
        """Met à jour la card récapitulatif et résout l'entry sélectionnée."""
        matches = _filter_entries()
        if state["marque"] and state["bottle"] and state["gout"] and matches:
            entry = matches[0]
            state["entry"] = entry
            recap_label.text = entry.designation
            recap_label.style(f"color: {COLORS['ink']}; font-weight: 600")
            recap_details.clear()
            with recap_details:
                _kv("Code interne", entry.code_interne or "—")
                _kv("GTIN colis (EAN)", entry.ean_colis)
                _kv("Lot", entry.lot_str)
                _kv("DDM", entry.ddm_date.strftime("%d/%m/%Y"))
                _kv("Format", f"{entry.fmt} (PCB {entry.pcb})")
            recap_details.set_visibility(True)
            # Borner les inputs partial selon le layout du format
            layout = get_palette_layout(entry.fmt, entry.product_label)
            layers_input.props(f"max={layout['layers']}")
            extras_input.props(f"max={max(0, layout['per_layer'] - 1)}")
            layers_label.text = f"Étages pleins (max {layout['layers']})"
            extras_label.text = (
                f"Caisses sur le dernier étage (max {layout['per_layer'] - 1})"
            )
        else:
            state["entry"] = None
            recap_label.text = (
                "Sélectionne marque, bouteille et goût pour voir les détails du produit."
            )
            recap_label.style(f"color: {COLORS['ink2']}")
            recap_details.set_visibility(False)
        _refresh_total()

    def _refresh_total():
        entry: LabelEntry | None = state["entry"]
        if not entry:
            total_display.text = "Total : —"
            generate_btn.disable()
            return
        try:
            count = compute_case_count(
                entry.fmt,
                full_pallet=bool(full_pallet_toggle.value),
                layers_full=int(layers_input.value or 0),
                extras_top=int(extras_input.value or 0),
                product_label=entry.product_label,
            )
        except ValueError as exc:
            total_display.text = f"⚠ {exc}"
            generate_btn.disable()
            return
        total_display.text = f"Total : {count} caisses"
        generate_btn.enable()

    def _on_marque_click(m: str):
        state["marque"] = m
        # Reset des étapes suivantes
        state["bottle"] = None
        state["gout"] = None
        _set_active_button(marque_buttons, m)
        _set_active_button(bottle_buttons, None)
        _refresh_bottles()
        _refresh_gouts()
        _refresh_recap()

    def _on_bottle_click(bt: str):
        if not state["marque"]:
            ui.notify("Sélectionne d'abord la marque.", type="warning")
            return
        state["bottle"] = bt
        state["gout"] = None
        _set_active_button(bottle_buttons, bt)
        _refresh_gouts()
        _refresh_recap()

    def _on_gout_change(e):
        state["gout"] = e.value
        _refresh_recap()

    def _on_full_pallet_toggle(e):
        partial_container.set_visibility(not e.value)
        _refresh_total()

    def _on_layers_change(_e):
        _refresh_total()

    def _on_extras_change(_e):
        _refresh_total()

    for m, btn in marque_buttons.items():
        btn.on_click(lambda _e, mm=m: _on_marque_click(mm))
    for bt, btn in bottle_buttons.items():
        btn.on_click(lambda _e, b=bt: _on_bottle_click(b))
    gout_select.on_value_change(_on_gout_change)
    full_pallet_toggle.on_value_change(_on_full_pallet_toggle)
    layers_input.on_value_change(_on_layers_change)
    extras_input.on_value_change(_on_extras_change)

    async def _on_generate():
        entry: LabelEntry | None = state["entry"]
        if not entry:
            ui.notify("Sélectionne marque, bouteille et goût.", type="warning")
            return
        try:
            count = compute_case_count(
                entry.fmt,
                full_pallet=bool(full_pallet_toggle.value),
                layers_full=int(layers_input.value or 0),
                extras_top=int(extras_input.value or 0),
                product_label=entry.product_label,
            )
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return

        generate_btn.disable()
        generate_btn.props("loading")
        try:
            from common.etiquette_palette_pdf import (
                EtiquetteContext,
                build_etiquette_palette_pdf,
            )

            ctx = EtiquetteContext(
                product_label=entry.product_label,
                fmt=entry.fmt,
                ean13=entry.ean_colis,
                lot=entry.lot_str,
                ddm=entry.ddm_date,
                case_count=count,
                full_pallet=bool(full_pallet_toggle.value),
                tenant_name=tenant_name,
            )
            pdf_bytes = await asyncio.to_thread(build_etiquette_palette_pdf, ctx)
            fname = (
                f"etiquette_{entry.marque}_{entry.fmt}_"
                f"{entry.gout.replace(' ', '_')}_{entry.lot_str}_{count}c.pdf"
            )
            ui.download(pdf_bytes, fname)
            ui.notify(
                "Étiquette générée — ouvre-la et imprime via AirPrint.",
                type="positive",
                icon="check",
            )
        except Exception as exc:
            _log.exception("Erreur génération PDF étiquette palette")
            ui.notify(f"Erreur génération PDF : {exc}", type="negative")
        finally:
            generate_btn.enable()
            generate_btn.props(remove="loading")

    generate_btn.on_click(_on_generate)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _kv(label: str, value: str) -> None:
    """Affiche une ligne 'label : value' dans le récap."""
    with ui.row().classes("w-full gap-2"):
        ui.label(f"{label} :").classes("text-body2").style(
            f"color: {COLORS['ink2']}; min-width: 140px",
        )
        ui.label(value).classes("text-body2").style(
            f"color: {COLORS['ink']}; font-weight: 500",
        )


def _resolve_tenant_name() -> str:
    """Résout le nom du tenant pour l'afficher en footer du PDF."""
    try:
        from common._session import current_tenant_id
        from db.conn import run_sql
        tid = current_tenant_id()
        rows = run_sql("SELECT name FROM tenants WHERE id = :t", {"t": tid}) or []
        if rows:
            return str(rows[0].get("name") or "")
    except Exception:
        _log.debug("Impossible de résoudre le nom tenant", exc_info=True)
    return ""
