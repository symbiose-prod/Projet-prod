"""
pages/etiquettes_palette.py
============================
Page d'édition d'étiquettes palette logistique avec code-barres GS1-128.

Mode scan-first :
  1. L'opérateur tape sur « Scanner un carton » (bouton hero vert).
  2. La caméra iOS native s'ouvre via ``<input capture="environment">``.
  3. La photo est resizée côté client (canvas 1280px max) puis uploadée à
     ``/api/scan-barcode``.
  4. Le serveur décode le GS1-128 (zxing-cpp), extrait EAN/lot/DDM, puis
     interroge la matrice codes-barres EasyBeer (cache 24 h) pour résoudre
     marque/format/PCB/désignation/goût.
  5. Tout est pré-rempli dans le récap (avec photo produit). L'opérateur
     n'a plus qu'à cocher palette pleine / saisir le détail des étages,
     puis générer le PDF (1 ou 2 exemplaires selon recommandation GS1).

Fallback : si le scan échoue ou que l'EAN n'est pas dans la matrice EB,
l'opérateur peut ouvrir la cascade « Sélection manuelle » (collapsée par
défaut) ou saisir l'EAN à la main. La cascade est alimentée par la sync
étiquettes (table ``sync_operations``) — moins fraîche que la matrice EB.

PDF : 102×152 mm (Dymo 5XL Wireless), AirPrint depuis l'iPad.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging

from nicegui import ui

from common.ramasse import get_palette_layout
from common.services.etiquette_palette_service import (
    BOTTLE_TYPES,
    HistoryEntry,
    LabelEntry,
    SyncStatus,
    compute_case_count,
    find_entry_by_ean,
    get_product_image_url,
    get_sync_status,
    list_recent_labels,
    load_label_data_from_sync,
    save_label_history,
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

        # Mode scan-first : on ne déclenche PAS de sync au boot. Le scan
        # interroge directement la matrice codes-barres EasyBeer (cache 24 h),
        # qui contient tous les produits déclarés. La sync étiquettes ne sert
        # plus qu'à alimenter le formulaire de saisie manuelle (fallback).
        try:
            entries, info_msg = await asyncio.to_thread(load_label_data_from_sync, tenant_id)
            status = await asyncio.to_thread(get_sync_status, tenant_id)
        except Exception as exc:
            _log.exception("Erreur chargement payload sync étiquettes")
            error_banner(f"Impossible de charger les données : {exc}", dismissible=False)
            return

        if info_msg:
            error_banner(info_msg, dismissible=True)

        _render_form(
            entries or [],
            tenant_name=user.get("tenant_name") or _resolve_tenant_name(),
            tenant_id=tenant_id,
            user_email=user.get("email", ""),
            sync_status=status,
        )


# ─── UI principale ──────────────────────────────────────────────────────────

def _render_form(
    entries: list[LabelEntry],
    tenant_name: str = "",
    tenant_id: str = "",
    user_email: str = "",
    sync_status: SyncStatus | None = None,
) -> None:
    """Rend le formulaire scan-first : bouton hero scanner, photo produit dans
    le récap, cascade marque/bouteille/goût en mode fallback (collapsed)."""
    state: dict = {
        "marque": None,
        "bottle": None,
        "gout": None,
        "entry": None,
    }

    # ────────────────────────────────────────────────────────────────────
    # Sync bar (discret) — affichée uniquement si une sync existe.
    # En mode scan-first, la sync sert seulement au formulaire de saisie
    # manuelle. Le scan interroge la matrice EasyBeer directement.
    # ────────────────────────────────────────────────────────────────────
    if sync_status and sync_status.has_sync and tenant_id:
        _render_sync_bar(tenant_id, sync_status)

    # ────────────────────────────────────────────────────────────────────
    # HERO : Scanner un carton (caméra iOS native via <label>+<input>)
    # ────────────────────────────────────────────────────────────────────
    with ui.row().classes("w-full justify-center q-mt-md q-mb-sm"):
        ui.html(
            '<label '
            'style="display:inline-flex; align-items:center; gap:12px; '
            'padding:22px 36px; background:#15803D; color:white; '
            'border-radius:6px; cursor:pointer; font-size:14px; '
            'border-radius:12px; cursor:pointer; font-size:20px; '
            'font-weight:600; user-select:none; position:relative; '
            'overflow:hidden; box-shadow:0 4px 12px rgba(21,128,61,0.3); '
            '-webkit-tap-highlight-color: rgba(255,255,255,0.2);">'
            '<span class="material-icons" style="font-size:32px;">qr_code_scanner</span>'
            'Scanner un carton'
            '<input type="file" id="photo-capture-input" '
            'accept="image/*" capture="environment" '
            'style="position:absolute; inset:0; opacity:0; cursor:pointer; '
            'width:100%; height:100%;">'
            '</label>',
        )

    with ui.row().classes("w-full justify-center q-mb-md"):
        ui.button(
            "Saisir l'EAN à la main",
            icon="keyboard",
            on_click=lambda: _open_manual_ean_dialog(_handle_scanned_data),
        ).props("flat color=grey-7 dense")

    # Listener JS du file input — resize côté client (canvas 1280px max) avant
    # upload pour réduire la latence de 3-5 s sur 4G à <1 s, sans perte de
    # qualité utile pour zxing-cpp. Émet l'EAN décodé via emitEvent.
    ui.add_body_html("""
    <script>
    (function() {
        if (window._fsScanInputBound) return;
        window._fsScanInputBound = true;

        // Resize via canvas : conserve l'aspect ratio, max 1280px sur le grand
        // côté, JPEG 85% (qualité largement suffisante pour décodage barcode).
        async function _fsResizeImage(file, maxDim) {
            return new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onerror = () => reject(new Error('FileReader'));
                reader.onload = () => {
                    const img = new Image();
                    img.onerror = () => reject(new Error('Image load'));
                    img.onload = () => {
                        let w = img.naturalWidth, h = img.naturalHeight;
                        const scale = Math.min(1, maxDim / Math.max(w, h));
                        w = Math.round(w * scale);
                        h = Math.round(h * scale);
                        const canvas = document.createElement('canvas');
                        canvas.width = w; canvas.height = h;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0, w, h);
                        canvas.toBlob(
                            (blob) => blob ? resolve(blob)
                                : reject(new Error('toBlob')),
                            'image/jpeg', 0.85,
                        );
                    };
                    img.src = reader.result;
                };
                reader.readAsDataURL(file);
            });
        }

        const wait = () => {
            const input = document.getElementById('photo-capture-input');
            if (!input) { setTimeout(wait, 200); return; }
            input.addEventListener('change', async (e) => {
                const file = e.target.files && e.target.files[0];
                if (!file) return;
                emitEvent('barcode_uploading', file.size);
                try {
                    let toUpload;
                    try {
                        toUpload = await _fsResizeImage(file, 1280);
                    } catch (err) {
                        // Fallback : envoie le fichier original si le resize
                        // échoue (HEIC pur que le navigateur ne décode pas)
                        console.warn('resize failed, sending original', err);
                        toUpload = file;
                    }
                    const formData = new FormData();
                    formData.append('file', toUpload, 'photo.jpg');
                    const resp = await fetch('/api/scan-barcode', {
                        method: 'POST', body: formData,
                    });
                    const data = await resp.json();
                    if (data.ean) {
                        emitEvent('barcode_scanned', data);
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
    # Récapitulatif produit (photo + détails) — visible dès qu'un scan a lieu
    # ────────────────────────────────────────────────────────────────────
    section_title("Produit identifié", "info")
    recap_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
    with recap_card:
        with ui.row().classes("w-full items-center gap-4 no-wrap"):
            recap_image = ui.image("").classes("rounded").style(
                "width:84px; height:84px; object-fit:cover; "
                "background:#f3f4f6; border:1px solid " + COLORS["border"],
            )
            recap_image.set_visibility(False)
            with ui.column().classes("gap-0 flex-1"):
                recap_label = ui.label(
                    "Scanne un carton ou saisis l'EAN pour identifier le produit.",
                ).classes("text-body1").style(f"color: {COLORS['ink2']}")
                recap_details = ui.column().classes("w-full gap-1 q-mt-xs")
                recap_details.set_visibility(False)

    # ────────────────────────────────────────────────────────────────────
    # Saisie manuelle (cascade marque/bouteille/goût) — collapsée par défaut
    # ────────────────────────────────────────────────────────────────────
    marques_dispo = sorted({e.marque for e in entries})
    marque_buttons: dict[str, ui.button] = {}
    bottle_buttons: dict[str, ui.button] = {}
    with ui.expansion(
        text="Sélection manuelle (marque / bouteille / goût)",
        icon="tune",
    ).classes("w-full q-mb-sm").props("dense") as manual_expansion:
        # Step 1 — Marque
        section_title("Marque", "branding_watermark")
        marque_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
        with marque_card:
            with ui.row().classes("w-full gap-3"):
                for m in marques_dispo:
                    btn = ui.button(m).classes("flex-1").props("size=lg outline color=green-8")
                    marque_buttons[m] = btn

        # Step 2 — Type de bouteille
        section_title("Type de bouteille", "wine_bar")
        bottle_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
        with bottle_card:
            with ui.row().classes("w-full gap-3"):
                for bt in BOTTLE_TYPES:
                    btn = ui.button(bt).classes("flex-1").props("size=lg outline color=green-8")
                    btn.disable()
                    bottle_buttons[bt] = btn

        # Step 3 — Goût
        section_title("Goût", "local_drink")
        gout_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
        with gout_card:
            gout_select = ui.select(
                options=[],
                label="Choisir le goût",
                with_input=True,
            ).classes("w-full").props("outlined dense fill-input use-input input-debounce=0")
            gout_select.disable()
    _ = manual_expansion  # référence pour pouvoir l'ouvrir/fermer plus tard si besoin

    # ────────────────────────────────────────────────────────────────────
    # Quantité de caisses
    # ────────────────────────────────────────────────────────────────────
    section_title("Quantité de caisses", "inventory_2")
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
    # Génération du PDF — checkbox 2 exemplaires + bouton
    # ────────────────────────────────────────────────────────────────────
    with ui.row().classes("w-full items-center gap-3 q-mt-sm"):
        double_copies_toggle = ui.checkbox(
            "Imprimer 2 étiquettes (recommandé GS1 : 2 faces de palette)",
            value=True,
        )

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
            recap_label.style(f"color: {COLORS['ink']}; font-weight: 600; font-size: 17px")
            # Photo produit (depuis assets/) si goût mappé
            img_url = get_product_image_url(entry.gout)
            if img_url:
                recap_image.set_source(img_url)
                recap_image.set_visibility(True)
            else:
                recap_image.set_visibility(False)
            recap_details.clear()
            with recap_details:
                _kv("EAN", entry.ean_colis)
                _kv("Lot", entry.lot_str)
                _kv("DDM", entry.ddm_date.strftime("%d/%m/%Y"))
                _kv("Format", f"{entry.fmt} ({entry.pcb} btl/carton)")
            recap_details.set_visibility(True)
            layout = get_palette_layout(entry.fmt, entry.product_label)
            layers_input.props(f"max={layout['layers']}")
            extras_input.props(f"max={max(0, layout['per_layer'] - 1)}")
            layers_label.text = f"Étages pleins (max {layout['layers']})"
            extras_label.text = (
                f"Caisses sur le dernier étage (max {layout['per_layer'] - 1})"
            )
        else:
            recap_image.set_visibility(False)
            recap_label.text = (
                "Scanne un carton ou saisis l'EAN pour identifier le produit."
            )
            recap_label.style(f"color: {COLORS['ink2']}; font-weight: 400; font-size: 14px")
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
    ui.on(
        "barcode_uploading",
        lambda e: ui.notify(
            "📤 Décodage en cours…", type="info", timeout=2000,
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
                n_copies=2 if double_copies_toggle.value else 1,
            )
            pdf_bytes = await asyncio.to_thread(build_etiquette_palette_pdf, ctx)
            fname = (
                f"etiquette_{entry.marque}_{entry.fmt}_"
                f"{entry.gout.replace(' ', '_')}_{entry.lot_str}_{count}c.pdf"
            )
            ui.download(pdf_bytes, fname)
            # Audit + historique pour réimpression future (fire-and-forget)
            await asyncio.to_thread(
                save_label_history,
                tenant_id,
                user_email=user_email,
                ean=entry.ean_colis,
                lot=entry.lot_str,
                ddm=entry.ddm_date,
                fmt=entry.fmt,
                marque=entry.marque,
                designation=entry.designation,
                gout=entry.gout,
                case_count=count,
                full_pallet=bool(full_pallet_toggle.value),
                n_copies=ctx.n_copies,
                pcb=entry.pcb,
            )
            _refresh_history()
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

    # ────────────────────────────────────────────────────────────────────
    # Étiquettes récentes (historique pour réimpression et audit)
    # ────────────────────────────────────────────────────────────────────
    history_section = ui.column().classes("w-full q-mt-lg")

    async def _do_reprint(h: HistoryEntry):
        try:
            from common.etiquette_palette_pdf import (
                EtiquetteContext,
                build_etiquette_palette_pdf,
            )
            ctx = EtiquetteContext(
                product_label=h.designation or f"GTIN {h.ean}",
                fmt=h.fmt,
                ean13=h.ean,
                lot=h.lot,
                ddm=h.ddm,
                case_count=h.case_count,
                full_pallet=h.full_pallet,
                tenant_name=tenant_name,
                n_copies=h.n_copies,
            )
            pdf_bytes = await asyncio.to_thread(build_etiquette_palette_pdf, ctx)
            fname = (
                f"etiquette_REIMPR_{h.marque}_{h.fmt}_{h.lot}_{h.case_count}c.pdf"
            )
            ui.download(pdf_bytes, fname)
            ui.notify(
                f"✓ Étiquette « {h.designation or h.ean} » regénérée.",
                type="positive", icon="check",
            )
        except Exception as exc:
            _log.exception("Erreur réimpression étiquette palette")
            ui.notify(f"Erreur réimpression : {exc}", type="negative")

    def _refresh_history():
        """Recharge la section historique (appelée après chaque génération)."""
        history_section.clear()
        if not tenant_id:
            return
        recent = list_recent_labels(tenant_id, limit=10)
        if not recent:
            return
        with history_section:
            section_title("Étiquettes récentes", "history")
            with ui.card().classes("w-full q-pa-none").props("flat bordered"):
                for i, h in enumerate(recent):
                    if i > 0:
                        ui.separator()
                    with ui.card_section().classes("q-pa-sm"):
                        with ui.row().classes("w-full items-center gap-3 no-wrap"):
                            img_url = get_product_image_url(h.gout)
                            if img_url:
                                ui.image(img_url).classes("rounded").style(
                                    "width:48px; height:48px; object-fit:cover; "
                                    "background:#f3f4f6",
                                )
                            with ui.column().classes("gap-0 flex-1"):
                                title = h.designation or f"GTIN {h.ean}"
                                ui.label(f"{title} — {h.fmt}").classes(
                                    "text-body2",
                                ).style(f"color: {COLORS['ink']}; font-weight: 500")
                                meta = (
                                    f"Lot {h.lot} · DDM {h.ddm.strftime('%d/%m/%Y')} · "
                                    f"{h.case_count} caisses"
                                )
                                if h.n_copies > 1:
                                    meta += f" · {h.n_copies} ex."
                                ui.label(meta).classes("text-caption").style(
                                    f"color: {COLORS['ink2']}",
                                )
                                when = h.generated_at.strftime("%d/%m/%Y %H:%M") if hasattr(
                                    h.generated_at, "strftime",
                                ) else str(h.generated_at)
                                who = f" par {h.user_email}" if h.user_email else ""
                                ui.label(f"Imprimée le {when}{who}").classes(
                                    "text-caption",
                                ).style(f"color: {COLORS['ink2']}")
                            ui.button(
                                "Réimprimer", icon="print",
                                on_click=lambda _e, hh=h: _do_reprint(hh),
                            ).props("flat color=green-8 dense")

    _refresh_history()


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
