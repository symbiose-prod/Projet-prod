"""
pages/etiquettes_palette.py
============================
Page d'édition d'étiquettes palette logistique avec code-barres GS1-128.

Flux opérateur (iPad-friendly) :
  1. Sélection produit + format (chargés depuis EasyBeer)
  2. Sélection brassin actif → pré-remplit lot (= code brassin) et DDM
  3. Saisie de la quantité : palette pleine OU étages pleins + caisses dernière couche
  4. Génération du PDF (102×152 mm, format Dymo 5XL Wireless) → AirPrint

Le PDF est téléchargé directement par le navigateur. Sur iPad, l'opérateur
ouvre le PDF dans Safari et utilise le bouton de partage → "Imprimer" → AirPrint.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging

from nicegui import ui

from common.ramasse import get_palette_layout
from common.services.etiquette_palette_service import (
    EtiquettePaletteData,
    ProductFormat,
    compute_case_count,
    load_initial_data,
)
from pages.auth import require_auth
from pages.theme import COLORS, error_banner, page_layout, section_title

_log = logging.getLogger("ferment.etiquettes_palette")


@ui.page("/etiquettes-palette")
async def page_etiquettes_palette():
    user = require_auth()
    if not user:
        return

    with page_layout("Étiquettes palette", "qr_code_2", "/etiquettes-palette"):

        # ── Chargement initial des données EasyBeer (en thread, non-bloquant) ──
        ui.label(
            "Édite une étiquette logistique GS1-128 pour palette filmée — "
            "imprime via AirPrint depuis l'iPad."
        ).classes("text-body2").style(f"color: {COLORS['ink2']}")

        loading_card = ui.card().classes("w-full q-pa-lg items-center").props("flat bordered")
        with loading_card:
            ui.spinner("dots", size="lg", color="green-8")
            ui.label("Chargement des produits et brassins…").classes("text-body2 q-mt-sm")

        try:
            data: EtiquettePaletteData = await asyncio.to_thread(load_initial_data)
        except Exception as exc:
            _log.exception("Erreur chargement initial étiquettes palette")
            loading_card.delete()
            error_banner(f"Impossible de charger les données : {exc}", dismissible=False)
            return

        loading_card.delete()

        # Bandeaux d'erreur partielle (EasyBeer indisponible)
        for err in data.errors:
            error_banner(err, dismissible=True)

        if not data.products:
            error_banner(
                "Aucun produit disponible avec EAN. Vérifie la connexion EasyBeer "
                "et la matrice codes-barres.",
                dismissible=False,
            )
            return

        _render_form(data, tenant_name=user.get("tenant_name") or _resolve_tenant_name())


# ─── UI principale ──────────────────────────────────────────────────────────

def _render_form(data: EtiquettePaletteData, tenant_name: str = "") -> None:
    """Rend le formulaire en 4 sections (produit → brassin → quantité → action)."""
    state: dict = {
        "selected_product": None,    # ProductFormat
        "selected_brassin": None,    # BrassinChoice | None
        "lot_value": "",
        "ddm_value": "",             # ISO yyyy-mm-dd
        "full_pallet": True,
        "layers_full": 0,
        "extras_top": 0,
    }

    # Index produits par libellé pour le sélecteur Step 1
    products_by_label: dict[str, list[ProductFormat]] = {}
    for pf in data.products:
        products_by_label.setdefault(pf.libelle, []).append(pf)
    product_labels = sorted(products_by_label.keys(), key=str.lower)

    # ────────────────────────────────────────────────────────────────────
    # Step 1 — Produit + Format
    # ────────────────────────────────────────────────────────────────────
    section_title("1. Produit & format", "category")
    with ui.card().classes("w-full q-pa-md").props("flat bordered"):
        product_select = ui.select(
            options=product_labels,
            label="Produit",
        ).classes("w-full").props("outlined dense use-input input-debounce=0 fill-input")

        format_select = ui.select(
            options=[],
            label="Format de carton",
        ).classes("w-full q-mt-sm").props("outlined dense")
        format_select.disable()

        ean_label = ui.label("").classes("text-caption q-mt-xs").style(
            f"color: {COLORS['ink2']}"
        )

    # ────────────────────────────────────────────────────────────────────
    # Step 2 — Brassin (lot + DDM)
    # ────────────────────────────────────────────────────────────────────
    section_title("2. Brassin (lot & DDM)", "science")
    with ui.card().classes("w-full q-pa-md").props("flat bordered"):
        if data.brassins:
            brassin_options = {
                b.id_brassin: f"{b.code}  ·  {b.libelle_produit}"
                for b in data.brassins
            }
            brassin_select = ui.select(
                options=brassin_options,
                label="Brassin actif (pré-remplit lot et DDM)",
                with_input=True,
            ).classes("w-full").props("outlined dense fill-input use-input input-debounce=0")
        else:
            brassin_select = None
            ui.label(
                "Aucun brassin en cours détecté — saisis le lot manuellement."
            ).classes("text-caption").style(f"color: {COLORS['warning']}")

        with ui.row().classes("w-full gap-3 q-mt-sm"):
            lot_input = ui.input("Lot", placeholder="ex: KME27042026").classes("flex-1").props(
                "outlined dense"
            )
            ddm_input = ui.input("DDM (jj/mm/aaaa)", placeholder="01/09/2026").classes(
                "flex-1"
            ).props("outlined dense")
            with ddm_input:
                with ui.menu().props("no-parent-event") as ddm_menu:
                    ddm_picker = ui.date(mask="DD/MM/YYYY").props("dense first-day-of-week=1")
                    ddm_picker.on_value_change(
                        lambda e: (ddm_input.set_value(e.value), ddm_menu.close())
                    )
                with ddm_input.add_slot("append"):
                    ui.icon("event", size="xs").classes("cursor-pointer").on(
                        "click", lambda: ddm_menu.open(),
                    )

    # ────────────────────────────────────────────────────────────────────
    # Step 3 — Quantité
    # ────────────────────────────────────────────────────────────────────
    section_title("3. Quantité de caisses", "inventory_2")
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
    # Step 4 — Action
    # ────────────────────────────────────────────────────────────────────
    section_title("4. Génération de l'étiquette", "qr_code_2")
    with ui.row().classes("w-full gap-3"):
        generate_btn = ui.button(
            "Générer & télécharger le PDF",
            icon="picture_as_pdf",
        ).classes("flex-1").props("color=green-8 unelevated size=lg")
        generate_btn.disable()

    feedback_label = ui.label("").classes("text-body2 q-mt-xs")
    feedback_label.set_visibility(False)

    # ────────────────────────────────────────────────────────────────────
    # Logique réactive
    # ────────────────────────────────────────────────────────────────────

    def _refresh_total():
        pf: ProductFormat | None = state["selected_product"]
        if not pf:
            total_display.text = "Total : —"
            generate_btn.disable()
            return
        try:
            count = compute_case_count(
                pf.fmt,
                full_pallet=bool(full_pallet_toggle.value),
                layers_full=int(layers_input.value or 0),
                extras_top=int(extras_input.value or 0),
                product_label=pf.libelle,
            )
        except ValueError as exc:
            total_display.text = f"⚠ {exc}"
            generate_btn.disable()
            return
        total_display.text = f"Total : {count} caisses"
        # Activation bouton uniquement si données complètes
        if state["lot_value"] and state["ddm_value"]:
            generate_btn.enable()
        else:
            generate_btn.disable()

    def _on_product_change(e):
        label = e.value
        if not label or label not in products_by_label:
            format_select.options = []
            format_select.disable()
            ean_label.text = ""
            state["selected_product"] = None
            _refresh_total()
            return
        formats = products_by_label[label]
        format_select.options = {pf.fmt: f"{pf.fmt}cl" for pf in formats}
        format_select.value = None
        format_select.enable()
        ean_label.text = ""
        state["selected_product"] = None
        _refresh_total()

    def _on_format_change(e):
        label = product_select.value
        fmt = e.value
        if not (label and fmt):
            state["selected_product"] = None
            ean_label.text = ""
            _refresh_total()
            return
        match = next(
            (pf for pf in products_by_label.get(label, []) if pf.fmt == fmt),
            None,
        )
        state["selected_product"] = match
        if match:
            ean_label.text = f"EAN-13 caisse : {match.ean13}"
            # Borner les inputs partial_container selon le layout du format
            layout = get_palette_layout(match.fmt, match.libelle)
            layers_input.props(f"max={layout['layers']}")
            extras_input.props(f"max={max(0, layout['per_layer'] - 1)}")
            layers_label.text = f"Étages pleins (max {layout['layers']})"
            extras_label.text = (
                f"Caisses sur le dernier étage (max {layout['per_layer'] - 1})"
            )
        _refresh_total()

    def _on_brassin_change(e):
        if not e.value:
            return
        brassin = next(
            (b for b in data.brassins if b.id_brassin == e.value),
            None,
        )
        if not brassin:
            return
        state["selected_brassin"] = brassin
        # Pré-remplir lot et DDM (l'opérateur peut écraser)
        lot_input.set_value(brassin.code)
        ddm_input.set_value(brassin.ddm_date.strftime("%d/%m/%Y"))

    def _on_lot_change(e):
        state["lot_value"] = (e.value or "").strip()
        _refresh_total()

    def _on_ddm_change(e):
        state["ddm_value"] = (e.value or "").strip()
        _refresh_total()

    def _on_full_pallet_toggle(e):
        state["full_pallet"] = bool(e.value)
        partial_container.set_visibility(not e.value)
        _refresh_total()

    def _on_layers_change(_e):
        state["layers_full"] = int(layers_input.value or 0)
        _refresh_total()

    def _on_extras_change(_e):
        state["extras_top"] = int(extras_input.value or 0)
        _refresh_total()

    product_select.on_value_change(_on_product_change)
    format_select.on_value_change(_on_format_change)
    if brassin_select is not None:
        brassin_select.on_value_change(_on_brassin_change)
    lot_input.on("update:model-value", _on_lot_change)
    ddm_input.on("update:model-value", _on_ddm_change)
    full_pallet_toggle.on_value_change(_on_full_pallet_toggle)
    layers_input.on_value_change(_on_layers_change)
    extras_input.on_value_change(_on_extras_change)

    async def _on_generate():
        pf: ProductFormat | None = state["selected_product"]
        if not pf:
            ui.notify("Sélectionne un produit et un format.", type="warning")
            return
        lot = state["lot_value"]
        if not lot:
            ui.notify("Renseigne le lot.", type="warning")
            return
        ddm_iso = _parse_ddm(state["ddm_value"])
        if not ddm_iso:
            ui.notify("DDM invalide (format jj/mm/aaaa attendu).", type="warning")
            return

        try:
            count = compute_case_count(
                pf.fmt,
                full_pallet=bool(full_pallet_toggle.value),
                layers_full=int(layers_input.value or 0),
                extras_top=int(extras_input.value or 0),
                product_label=pf.libelle,
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
                product_label=pf.libelle,
                fmt=pf.fmt,
                ean13=pf.ean13,
                lot=lot,
                ddm=ddm_iso,
                case_count=count,
                full_pallet=bool(full_pallet_toggle.value),
                tenant_name=tenant_name,
            )
            pdf_bytes = await asyncio.to_thread(build_etiquette_palette_pdf, ctx)
            fname = (
                f"etiquette_palette_{pf.fmt}_{lot}_{ddm_iso:%Y-%m-%d}_{count}.pdf"
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

def _parse_ddm(value: str) -> _dt.date | None:
    """Parse une DDM saisie au format jj/mm/aaaa → date, ou None si invalide."""
    s = (value or "").strip()
    if not s:
        return None
    try:
        return _dt.datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        try:
            return _dt.date.fromisoformat(s)
        except ValueError:
            return None


def _resolve_tenant_name() -> str:
    """Tente de résoudre le nom du tenant pour l'afficher en footer du PDF."""
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
