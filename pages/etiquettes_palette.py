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
import datetime as _dt
import logging

from nicegui import ui

from common.ramasse import get_palette_layout
from common.services.etiquette_palette_service import (
    BOTTLE_TYPES,
    LabelEntry,
    SyncStatus,
    compute_case_count,
    find_entry_by_ean,
    get_sync_status,
    load_label_data_from_sync,
    trigger_sync_now,
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
            spinner = ui.spinner("dots", size="lg", color="green-8")  # noqa: F841
            loading_label = ui.label("Chargement des produits…").classes("text-body2 q-mt-sm")

        # ── Si aucune sync n'existe, en lancer une automatiquement ──────────
        try:
            status = await asyncio.to_thread(get_sync_status, tenant_id)
        except Exception as exc:
            _log.exception("Erreur lecture statut sync")
            loading_card.delete()
            error_banner(f"Impossible de vérifier le statut sync : {exc}", dismissible=False)
            return

        if not status.has_sync:
            loading_label.text = (
                "Première utilisation — collecte des produits depuis EasyBeer "
                "(ça peut prendre 1-2 min)…"
            )
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(trigger_sync_now, tenant_id),
                    timeout=180,
                )
            except TimeoutError:
                loading_card.delete()
                error_banner(
                    "La sync EasyBeer a dépassé 3 min. Réessaie ou lance la sync "
                    "depuis Paramètres → Étiquettes.",
                    dismissible=False,
                )
                return
            except Exception as exc:
                _log.exception("Erreur sync auto au premier chargement")
                loading_card.delete()
                error_banner(f"Erreur sync auto : {exc}", dismissible=False)
                return

        # ── Charger le payload (existant ou fraîchement créé) ──────────────
        try:
            entries, info_msg = await asyncio.to_thread(load_label_data_from_sync, tenant_id)
            status = await asyncio.to_thread(get_sync_status, tenant_id)
        except Exception as exc:
            _log.exception("Erreur chargement payload sync étiquettes")
            loading_card.delete()
            error_banner(f"Impossible de charger les données : {exc}", dismissible=False)
            return

        loading_card.delete()

        if info_msg:
            error_banner(info_msg, dismissible=True)

        # ── Bandeau statut sync + bouton rafraîchir ────────────────────────
        _render_sync_bar(tenant_id, status)

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
    # Scanner caméra : un seul tap → caméra iOS native (capture="environment")
    # → photo HD → POST /api/scan-barcode → zxing-cpp côté serveur → emitEvent.
    # Pattern <input> imbriqué dans <label> : robuste cross-browser sans JS.
    # ────────────────────────────────────────────────────────────────────
    with ui.row().classes("w-full justify-end q-mb-sm gap-2 items-center"):
        ui.html(
            '<label '
            'style="display:inline-flex; align-items:center; gap:8px; '
            'padding:10px 18px; background:#15803D; color:white; '
            'border-radius:6px; cursor:pointer; font-size:14px; '
            'font-weight:500; user-select:none; position:relative; '
            'overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.2); '
            '-webkit-tap-highlight-color: rgba(255,255,255,0.2);">'
            '<span class="material-icons" style="font-size:20px;">qr_code_scanner</span>'
            'Scanner un carton'
            '<input type="file" id="photo-capture-input" '
            'accept="image/*" capture="environment" '
            'style="position:absolute; inset:0; opacity:0; cursor:pointer; '
            'width:100%; height:100%;">'
            '</label>',
        )
        # Bouton secondaire : saisie manuelle (au cas où le scan ne matche rien)
        ui.button(
            "Saisir EAN",
            icon="keyboard",
            on_click=lambda: _open_manual_ean_dialog(_handle_scanned_data),
        ).props("flat color=grey-7 dense")

    # Listener JS du file input — installé une fois, écoute le change,
    # upload au serveur et émet l'EAN décodé via emitEvent (canal NiceGUI).
    ui.add_body_html("""
    <script>
    (function() {
        if (window._fsScanInputBound) return;
        window._fsScanInputBound = true;
        const wait = () => {
            const input = document.getElementById('photo-capture-input');
            if (!input) { setTimeout(wait, 200); return; }
            input.addEventListener('change', async (e) => {
                const file = e.target.files && e.target.files[0];
                if (!file) return;
                try {
                    const formData = new FormData();
                    formData.append('file', file, file.name || 'photo.jpg');
                    const resp = await fetch('/api/scan-barcode', {
                        method: 'POST', body: formData,
                    });
                    const data = await resp.json();
                    if (data.ean) {
                        emitEvent('barcode_scanned', data.ean);
                    } else {
                        emitEvent('barcode_error',
                            (data.error || 'Aucun code-barres détecté'));
                    }
                } catch (err) {
                    emitEvent('barcode_error', String(err));
                }
                e.target.value = '';
            });
        };
        wait();
    })();
    </script>
    """)

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
        """Met à jour la card récap. Préserve l'entry synthétique d'un scan."""
        # Cas 1 : entry déjà posée (par un scan EAN qui matche EasyBeer ou la
        # sync) et cohérente avec les sélecteurs courants → on la garde telle
        # quelle (avec lot/DDM scannés).
        existing = state.get("entry")
        entry: LabelEntry | None = None
        if (
            existing is not None
            and state.get("marque") == existing.marque
            and state.get("bottle") == existing.bottle_type
        ):
            entry = existing
        else:
            # Cas 2 : résoudre via la cascade sync (entries)
            matches = _filter_entries()
            if state["marque"] and state["bottle"] and state["gout"] and matches:
                entry = matches[0]
                state["entry"] = entry
            else:
                state["entry"] = None
                entry = None

        if entry:
            recap_label.text = entry.designation
            recap_label.style(f"color: {COLORS['ink']}; font-weight: 600")
            recap_details.clear()
            with recap_details:
                if entry.code_interne:
                    _kv("Code interne", entry.code_interne)
                _kv("GTIN colis (EAN)", entry.ean_colis)
                _kv("Lot", entry.lot_str)
                _kv("DDM", entry.ddm_date.strftime("%d/%m/%Y"))
                _kv("Format", f"{entry.fmt} (PCB {entry.pcb})")
            recap_details.set_visibility(True)
            layout = get_palette_layout(entry.fmt, entry.product_label)
            layers_input.props(f"max={layout['layers']}")
            extras_input.props(f"max={max(0, layout['per_layer'] - 1)}")
            layers_label.text = f"Étages pleins (max {layout['layers']})"
            extras_label.text = (
                f"Caisses sur le dernier étage (max {layout['per_layer'] - 1})"
            )
        else:
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

    def _apply_synthetic_entry(entry: LabelEntry):
        """Applique une LabelEntry directement dans le state + UI (sans cascade).

        Permet de pré-remplir tout depuis un scan EasyBeer sans dépendre de
        la sync étiquettes (le produit peut ne pas y être encore).
        """
        state["entry"] = entry
        state["marque"] = entry.marque
        state["bottle"] = entry.bottle_type
        state["gout"] = entry.gout
        # Mise à jour visuelle (en best-effort si les boutons existent)
        _set_active_button(marque_buttons, entry.marque)
        _refresh_bottles()
        # On force enable du bouton bouteille même si la sync ne le proposait pas
        if entry.bottle_type in bottle_buttons:
            bottle_buttons[entry.bottle_type].enable()
        _set_active_button(bottle_buttons, entry.bottle_type)
        _refresh_gouts()
        # Ajoute le goût scanné aux options s'il n'est pas déjà là
        current_options = list(gout_select.options or [])
        if entry.gout and entry.gout not in current_options:
            gout_select.options = current_options + [entry.gout]
        gout_select.value = entry.gout
        gout_select.enable()
        _refresh_recap()

    def _handle_scanned_data(data):
        """Traite les données scannées (dict ou string ean).

        Format attendu (objet) :
            {ean, lot, ddm, product?: {marque, fmt, pcb, bottle_type,
                                        designation, gout, ean_colis}}

        Si ``product`` présent (lookup matrice EasyBeer OK) → on construit
        une LabelEntry synthétique avec lot/DDM scannés et on auto-sélectionne.
        Sinon → match dans la sync (fallback) ou notification d'échec.
        """
        # Compatibilité ascendante : si on reçoit juste un EAN string
        if isinstance(data, str):
            data = {"ean": data, "lot": "", "ddm": None, "product": None}
        if not isinstance(data, dict):
            return
        ean = (data.get("ean") or "").strip()
        if not ean:
            return
        scan_lot = (data.get("lot") or "").strip()
        scan_ddm_iso = data.get("ddm")
        scan_ddm: _dt.date | None = None
        if scan_ddm_iso:
            try:
                scan_ddm = _dt.date.fromisoformat(str(scan_ddm_iso)[:10])
            except (ValueError, TypeError):
                scan_ddm = None

        product = data.get("product") or None

        if product and product.get("bottle_type"):
            # Construire un LabelEntry à partir des données EasyBeer + scan
            ddm = scan_ddm or _dt.date.today() + _dt.timedelta(days=365)
            lot = scan_lot or ddm.strftime("%d%m%Y")
            synth = LabelEntry(
                marque=product.get("marque") or "",
                bottle_type=product.get("bottle_type") or "",
                gout=product.get("gout") or "—",
                designation=product.get("designation") or f"GTIN {ean}",
                fmt=product.get("fmt") or "",
                pcb=int(product.get("pcb") or 0),
                ean_colis=product.get("ean_colis") or ean,
                ean_uvc="",
                code_interne="",
                lot_str=lot,
                ddm_date=ddm,
                product_label=product.get("designation") or "",
            )
            _apply_synthetic_entry(synth)
            ui.notify(
                f"✓ {synth.designation} — Lot {lot} — DDM {ddm.strftime('%d/%m/%Y')}",
                type="positive",
                icon="check",
                timeout=4000,
            )
            return

        # Pas de product (matrice EB) : fallback sur la sync étiquettes
        matched = find_entry_by_ean(entries, ean)
        if matched:
            # Override lot/DDM avec les données scannées si présentes
            if scan_ddm or scan_lot:
                lot = scan_lot or matched.lot_str
                ddm = scan_ddm or matched.ddm_date
                matched = LabelEntry(
                    **{**matched.__dict__, "lot_str": lot, "ddm_date": ddm},
                )
            _apply_synthetic_entry(matched)
            ui.notify(
                f"✓ {matched.designation}",
                type="positive",
                icon="check",
                timeout=3000,
            )
            return

        ui.notify(
            f"Produit avec EAN {ean} introuvable (ni matrice EasyBeer, ni sync).",
            type="warning",
            timeout=6000,
        )

    # Events JS → Python via canal WebSocket NiceGUI (emitEvent côté JS)
    ui.on("barcode_scanned", lambda e: _handle_scanned_data(e.args))
    ui.on(
        "barcode_error",
        lambda e: ui.notify(
            f"Scan : {e.args}", type="warning", timeout=4000,
        ),
    )

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


# ─── Saisie manuelle EAN (fallback si scan échoue) ──────────────────────────

def _open_manual_ean_dialog(handler) -> None:
    """Petit dialog pour saisie manuelle d'un EAN, avec callback handler(ean).

    Utilisé quand le scan ne donne rien ou que l'EAN n'est pas dans la sync.
    """
    with ui.dialog() as dlg, ui.card().classes("q-pa-md").style("min-width: 320px"):
        ui.label("Saisie manuelle EAN").classes("text-subtitle1")
        ean_input = ui.input(
            placeholder="ex: 3770014427250",
        ).classes("w-full").props("outlined dense autofocus")

        def _submit():
            val = (ean_input.value or "").strip()
            if not val:
                return
            dlg.close()
            handler(val)

        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Annuler", on_click=dlg.close).props("flat color=grey-7")
            ui.button(
                "Valider", icon="check",
                on_click=_submit,
            ).props("color=green-8 unelevated")
        ean_input.on("keydown.enter", _submit)
    dlg.open()


# ─── Helpers ────────────────────────────────────────────────────────────────

def _render_sync_bar(tenant_id: str, status: SyncStatus) -> None:
    """Affiche l'âge de la dernière sync + un bouton 'Rafraîchir' qui relance."""
    age_label = _format_age(status.age_hours)
    color = _age_color(status.age_hours)

    with ui.card().classes("w-full").props("flat bordered"):
        with ui.card_section().classes("row items-center gap-3 q-pa-sm"):
            ui.icon("schedule", size="sm").style(f"color: {color}")
            with ui.column().classes("gap-0 flex-1"):
                ui.label(f"Données mises à jour {age_label}").classes(
                    "text-body2",
                ).style(f"color: {color}; font-weight: 500")
                ui.label(
                    f"{status.product_count} produits — statut : {status.status or '—'}",
                ).classes("text-caption").style(f"color: {COLORS['ink2']}")

            async def _do_refresh():
                refresh_btn.disable()
                refresh_btn.props("loading")
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(trigger_sync_now, tenant_id),
                        timeout=180,
                    )
                    if result.get("id"):
                        ui.notify(
                            f"Sync OK — {result['product_count']} produits. "
                            "Rechargement de la page…",
                            type="positive",
                        )
                        await asyncio.sleep(1)
                        ui.navigate.to("/etiquettes-palette")
                    else:
                        ui.notify(
                            "Aucun brassin en cours détecté — sync vide.",
                            type="warning",
                        )
                except TimeoutError:
                    ui.notify("La sync a dépassé 3 min. Réessaie.", type="negative")
                except Exception as exc:
                    _log.exception("Erreur sync manuelle")
                    ui.notify(f"Erreur sync : {exc}", type="negative")
                finally:
                    refresh_btn.enable()
                    refresh_btn.props(remove="loading")

            refresh_btn = ui.button(
                "Rafraîchir",
                icon="refresh",
                on_click=_do_refresh,
            ).props("outline color=green-8 dense")


def _format_age(age_hours: float | None) -> str:
    """Formate l'âge en string lisible : 'à l'instant', 'il y a 3h', 'il y a 2j'."""
    if age_hours is None:
        return "(date inconnue)"
    if age_hours < 1.0:
        mins = max(1, int(age_hours * 60))
        return f"il y a {mins} min"
    if age_hours < 24.0:
        return f"il y a {int(age_hours)}h"
    days = int(age_hours / 24)
    return f"il y a {days}j"


def _age_color(age_hours: float | None) -> str:
    """Couleur selon l'âge : vert < 12h, ambre < 36h, rouge ≥ 36h."""
    if age_hours is None or age_hours >= 36:
        return COLORS["error"]
    if age_hours >= 12:
        return COLORS["warning"]
    return COLORS["success"]


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
