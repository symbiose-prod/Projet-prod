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
import dataclasses
import datetime as _dt
import logging
import re

from nicegui import ui

from common.ramasse import get_palette_layout
from common.services.etiquette_palette_service import (
    BOTTLE_TYPES,
    HistoryEntry,
    LabelEntry,
    compute_case_count,
    find_entry_by_ean,
    get_product_image_url,
    list_recent_labels,
    load_label_data_from_sync,
    lookup_product_by_ean,
    purge_old_label_history,
    save_label_history,
)
from pages.auth import require_auth
from pages.theme import COLORS, page_layout, section_title

_log = logging.getLogger("ferment.etiquettes_palette")


def _step_title(step_num: int, title: str, icon: str = "") -> None:
    """Titre de section avec badge numéroté (1/2/3/4) — utilisé pour le
    flow wizard de la page : l'opérateur voit clairement quelle étape il
    est en train de remplir."""
    with ui.element("div").classes("section-header row items-center gap-2"):
        # Pastille verte avec le numéro de step
        with ui.element("div").style(
            f"width: 28px; height: 28px; border-radius: 50%; "
            f"background: {COLORS['green']}; color: white; "
            "display: inline-flex; align-items: center; justify-content: center; "
            "font-weight: 700; font-size: 14px; flex-shrink: 0",
        ):
            ui.label(str(step_num))
        if icon:
            ui.icon(icon, size="xs").style(f"color: {COLORS['green']}")
        ui.label(title).classes("text-subtitle1").style(
            f"color: {COLORS['ink']}; font-weight: 600",
        )


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

        # Mode scan-first : le scan interroge directement la matrice codes-barres
        # EasyBeer (cache 24 h). La sync étiquettes alimente uniquement le
        # formulaire de saisie manuelle (fallback) — on la charge mais sans
        # bloquer l'UI si elle est vide.
        try:
            entries, _info_msg = await asyncio.to_thread(load_label_data_from_sync, tenant_id)
        except Exception as exc:
            _log.exception("Erreur chargement payload sync étiquettes")
            entries = []
            _log.warning("Sync étiquettes indisponible : %s — page utilisable via scan", exc)

        _render_form(
            entries or [],
            tenant_name=user.get("tenant_name") or _resolve_tenant_name(),
            tenant_id=tenant_id,
            user_email=user.get("email", ""),
        )


# ─── UI principale ──────────────────────────────────────────────────────────

def _render_form(
    entries: list[LabelEntry],
    tenant_name: str = "",
    tenant_id: str = "",
    user_email: str = "",
) -> None:
    """Rend le formulaire scan-first : bouton hero scanner, photo produit dans
    le récap, cascade marque/bouteille/goût en mode fallback (collapsed)."""
    state: dict = {
        "marque": None,
        "bottle": None,
        "gout": None,
        "entry": None,
        # full_pallet : True | False | None (= pas encore choisi).
        # On force l'opérateur à choisir explicitement la première fois pour
        # éviter qu'il imprime "126 caisses palette pleine" sur une palette
        # incomplète sans l'avoir vu. Le choix se conserve ensuite à travers
        # les scans (séries de palettes du même produit).
        "full_pallet": None,
    }

    # ════════════════════════════════════════════════════════════════════
    # FLOW WIZARD — 4 étapes progressives.
    # Étape 1 (scan) : toujours visible.
    # Étapes 2/3/4 (produit/qty/imprimer) : révélées au fur et à mesure.
    # On masque les étapes en avance pour focaliser l'opérateur sur
    # l'action courante. Reset complet sur "Scanner le suivant".
    # ════════════════════════════════════════════════════════════════════

    async def _handle_manual_ean(ean: str):
        """Saisie manuelle EAN : interroge d'abord la matrice EasyBeer (source
        fraîche, cache 24 h) avant de tomber dans le fallback sync étiquettes.

        Sans cette étape, un produit ajouté récemment côté EasyBeer mais pas
        encore syncé serait introuvable malgré l'API qui le connaît.
        """
        cleaned = (ean or "").strip()
        if not cleaned:
            return
        ui.notify("🔍 Recherche du produit…", type="info", timeout=2000)
        try:
            product = await asyncio.to_thread(lookup_product_by_ean, cleaned)
        except Exception:
            _log.exception("Erreur lookup matrice EasyBeer (saisie manuelle)")
            product = None
        _handle_scanned_data({
            "ean": cleaned, "lot": "", "ddm": None, "product": product,
        })

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 1 — Scanner un carton (toujours visible)
    # ────────────────────────────────────────────────────────────────────
    step1_section = ui.column().classes("w-full")
    with step1_section:
        _step_title(1, "Scanner un carton", "qr_code_scanner")
        with ui.row().classes("w-full justify-center q-mt-sm q-mb-sm"):
            ui.html(
                '<label '
                'style="display:inline-flex; align-items:center; gap:12px; '
                'padding:22px 36px; background:#15803D; color:white; '
                'border-radius:12px; cursor:pointer; font-size:20px; '
                'font-weight:600; user-select:none; position:relative; '
                'overflow:hidden; box-shadow:0 4px 12px rgba(21,128,61,0.3); '
                '-webkit-tap-highlight-color: rgba(255,255,255,0.2); '
                'touch-action: manipulation;">'
                '<span class="material-icons" style="font-size:32px;">qr_code_scanner</span>'
                'Scanner un carton'
                '<input type="file" id="photo-capture-input" '
                'accept="image/*" capture="environment" '
                'style="position:absolute; inset:0; opacity:0; cursor:pointer; '
                'width:100%; height:100%;">'
                '</label>',
            )

        with ui.row().classes("w-full justify-center q-mb-sm"):
            ui.button(
                "Saisir l'EAN à la main",
                icon="keyboard",
                on_click=lambda: _open_manual_ean_dialog(_handle_manual_ean),
            ).props("outline color=grey-8")

        # Fallback discret : la cascade marque/bouteille/goût pour les cas
        # extrêmes où ni le scan ni l'EAN manuel ne donnent rien (carton
        # totalement abîmé, produit ajouté côté EB mais EAN absent).
        marques_dispo = sorted({e.marque for e in entries})
        marque_buttons: dict[str, ui.button] = {}
        bottle_buttons: dict[str, ui.button] = {}
        with ui.expansion(
            text="Tu ne trouves pas ton produit ? Sélection manuelle",
            icon="tune",
        ).classes("w-full q-mb-sm").props("dense") as manual_expansion:
            section_title("Marque", "branding_watermark")
            marque_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
            with marque_card:
                with ui.row().classes("w-full gap-3"):
                    for m in marques_dispo:
                        btn = ui.button(m).classes("flex-1").props(
                            "size=lg outline color=green-8",
                        )
                        marque_buttons[m] = btn

            section_title("Type de bouteille", "wine_bar")
            bottle_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
            with bottle_card:
                with ui.row().classes("w-full gap-3"):
                    for bt in BOTTLE_TYPES:
                        btn = ui.button(bt).classes("flex-1").props(
                            "size=lg outline color=green-8",
                        )
                        btn.disable()
                        bottle_buttons[bt] = btn

            section_title("Goût", "local_drink")
            gout_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
            with gout_card:
                gout_select = ui.select(
                    options=[],
                    label="Choisir le goût",
                    with_input=True,
                ).classes("w-full").props(
                    "outlined dense fill-input use-input input-debounce=0",
                )
                gout_select.disable()
        _ = manual_expansion

    _install_scan_input_listener()

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 2 — Produit identifié (révélée après scan/EAN OK)
    # ────────────────────────────────────────────────────────────────────
    produit_section = ui.column().classes("w-full q-mt-md")
    produit_section.props('id="step-produit"')
    with produit_section:
        _step_title(2, "Produit identifié", "info")
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
            # Bandeau d'alerte DDM dépassée — rendu plus visible qu'une notify
            # éphémère. L'opérateur le voit jusqu'au prochain scan.
            ddm_warning = ui.row().classes("w-full items-center gap-2 q-mt-sm q-pa-sm").style(
                "background:#FEF2F2; border:1px solid #FCA5A5; border-radius:6px",
            )
            with ddm_warning:
                ui.icon("warning", size="sm").style("color:#B91C1C")
                ddm_warning_label = ui.label("").classes("text-body2").style(
                    "color:#7F1D1D; font-weight:600",
                )
            ddm_warning.set_visibility(False)
    produit_section.set_visibility(False)

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 3 — Quantité de caisses (révélée si DDM OK après produit)
    # ────────────────────────────────────────────────────────────────────
    qty_section = ui.column().classes("w-full q-mt-md")
    qty_section.props('id="step-qty"')
    with qty_section:
        _step_title(3, "Quantité de caisses", "inventory_2")
        qty_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
        with qty_card:
            ui.label(
                "Choisis le type de palette :",
            ).classes("text-body2 q-mb-xs").style(f"color: {COLORS['ink2']}")
            with ui.row().classes("w-full gap-3 no-wrap"):
                full_pallet_btn = ui.button(
                    "PALETTE PLEINE", icon="inventory_2",
                ).classes("flex-1").props(
                    "size=lg outline color=green-8",
                ).style("min-height: 64px; touch-action: manipulation")
                partial_pallet_btn = ui.button(
                    "PALETTE PARTIELLE", icon="layers",
                ).classes("flex-1").props(
                    "size=lg outline color=orange-8",
                ).style("min-height: 64px; touch-action: manipulation")

            partial_container = ui.column().classes("w-full gap-3 q-mt-md")
            with partial_container:
                layers_label = ui.label("Tape sur l'étage le plus haut qui est complet :").classes(
                    "text-body2",
                ).style(f"color: {COLORS['ink']}; font-weight: 500")

                # Diagramme palette : empilement de boutons-étages cliquables.
                # Reconstruit dynamiquement à chaque scan (le format change).
                layers_diagram = ui.column().classes("w-full gap-1").style("max-width: 360px")
                no_layer_btn = ui.button(
                    "Aucun étage rempli",
                ).classes("w-full q-mt-xs").props("flat color=grey-7 size=sm")

                ui.separator().classes("q-my-sm")

                extras_label = ui.label(
                    "Caisses sur le dessus (étage incomplet) :",
                ).classes("text-body2").style(f"color: {COLORS['ink']}; font-weight: 500")
                with ui.row().classes("w-full items-center justify-center gap-3 q-mt-xs"):
                    # touch-action: manipulation → désactive le double-tap-to-zoom
                    # iOS Safari sur les taps répétés (+/-).
                    extras_minus_btn = ui.button("−").props(
                        "size=lg color=grey-8 round outline",
                    ).style(
                        "min-width: 56px; min-height: 56px; font-size: 28px; "
                        "touch-action: manipulation",
                    )
                    extras_value_label = ui.label("0").style(
                        f"color: {COLORS['ink']}; font-weight: 700; "
                        "font-size: 36px; min-width: 64px; text-align: center",
                    )
                    extras_plus_btn = ui.button("+").props(
                        "size=lg color=green-8 round outline",
                    ).style(
                        "min-width: 56px; min-height: 56px; font-size: 28px; "
                        "touch-action: manipulation",
                    )
                    extras_max_label = ui.label("").classes("text-caption q-ml-md").style(
                        f"color: {COLORS['ink2']}",
                    )

                # State holders cachés : la logique réactive existante
                # (compute_case_count, _refresh_total) lit ces valeurs.
                layers_input = ui.number(value=0, min=0, max=7, step=1)
                layers_input.set_visibility(False)
                extras_input = ui.number(value=0, min=0, max=35, step=1)
                extras_input.set_visibility(False)

            partial_container.set_visibility(False)
            # Boutons-étages reconstruits dans _rebuild_layers_diagram (au scan)
            layer_buttons: list[tuple[int, ui.button]] = []

            ui.separator().classes("q-my-md")
            # Total surdimensionné — c'est LE chiffre que l'opérateur doit
            # vérifier avant d'imprimer. On veut qu'il soit impossible à rater.
            with ui.column().classes("w-full items-center gap-0"):
                ui.label("CARTONS SUR LA PALETTE").classes("text-caption").style(
                    f"color: {COLORS['ink2']}; letter-spacing: 1px; font-weight: 600",
                )
                total_display = ui.label("—").style(
                    f"color: {COLORS['ink2']}; font-weight: 700; "
                    "font-size: 56px; line-height: 1.1; text-align: center",
                )
                total_capacity_label = ui.label("").classes("text-caption").style(
                    f"color: {COLORS['ink2']}",
                )
    qty_section.set_visibility(False)

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 4 — Imprimer (révélée quand quantité valide + DDM OK)
    # ────────────────────────────────────────────────────────────────────
    generate_section = ui.column().classes("w-full q-mt-md")
    generate_section.props('id="step-generate"')
    with generate_section:
        _step_title(4, "Imprimer", "print")
        with ui.row().classes("w-full items-center gap-3 q-mt-sm"):
            double_copies_toggle = ui.checkbox(
                "Imprimer 2 étiquettes (recommandé GS1 : 2 faces de palette)",
                value=True,
            )

        with ui.row().classes("w-full gap-3"):
            generate_btn = ui.button(
                "Générer & télécharger le PDF",
                icon="picture_as_pdf",
            ).classes("flex-1").props(
                "color=green-8 unelevated size=lg",
            ).style("touch-action: manipulation")
            generate_btn.disable()

        # Bouton "Scanner le suivant" — apparaît après une impression réussie
        next_scan_row = ui.row().classes("w-full justify-center q-mt-sm")
        with next_scan_row:
            next_scan_btn = ui.button(
                "📷 Scanner le carton suivant",
                on_click=lambda: _reset_for_next_scan(),
            ).props("color=blue-7 outline size=md")
        next_scan_row.set_visibility(False)
    generate_section.set_visibility(False)

    # ────────────────────────────────────────────────────────────────────
    # Logique réactive
    # ────────────────────────────────────────────────────────────────────

    def _refresh_layers_visual():
        """Met à jour la couleur des boutons-étages selon layers_input.value."""
        k = int(layers_input.value or 0)
        for i, btn in layer_buttons:
            if i <= k:
                btn.props(remove="outline")
                btn.props("unelevated color=green-7")
            else:
                btn.props(remove="unelevated")
                btn.props("outline color=grey-7")

    def _set_layers_full(k: int):
        layers_input.value = int(k)
        _refresh_layers_visual()
        _refresh_total()

    def _refresh_extras_visual():
        v = int(extras_input.value or 0)
        extras_value_label.text = str(v)

    def _on_extras_minus():
        v = int(extras_input.value or 0)
        if v > 0:
            extras_input.value = v - 1
            _refresh_extras_visual()
            _refresh_total()

    def _on_extras_plus():
        entry: LabelEntry | None = state.get("entry")
        if not entry:
            return
        layout = get_palette_layout(entry.fmt, entry.product_label)
        max_v = max(0, int(layout.get("per_layer") or 0) - 1)
        v = int(extras_input.value or 0)
        if v < max_v:
            extras_input.value = v + 1
            _refresh_extras_visual()
            _refresh_total()

    def _rebuild_layers_diagram(layout: dict):
        """Reconstruit la pile de boutons-étages selon le format du produit."""
        layer_buttons.clear()
        layers_diagram.clear()
        n_layers = int(layout.get("layers") or 0)
        per_layer = int(layout.get("per_layer") or 0)
        if n_layers <= 0 or per_layer <= 0:
            return
        # Affichage top-down (étage le plus haut en premier visuellement),
        # mais le numéro d'étage croit de bas en haut (étage 1 = sol).
        with layers_diagram:
            for i in range(n_layers, 0, -1):
                # Label avec total cumulé : "1 étage · 36 caisses",
                # "2 étages · 72 caisses", etc. → l'opérateur lit directement
                # le total qu'il obtient en tapant sur ce bouton, sans
                # multiplier mentalement.
                cumul = i * per_layer
                etage_word = "étage" if i == 1 else "étages"
                btn = ui.button(
                    f"{i} {etage_word}  ·  {cumul} caisses",
                ).classes("w-full").props(
                    "outline color=grey-7 size=md align=left",
                ).style(
                    "min-height: 44px; font-weight: 500; "
                    "touch-action: manipulation",
                )
                btn.on_click(lambda _e, k=i: _set_layers_full(k))
                layer_buttons.append((i, btn))
        extras_max_label.text = f"max {per_layer - 1}"
        # Reset visuel cohérent avec layers_input.value courant
        _refresh_layers_visual()
        _refresh_extras_visual()

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
            ddm_passed = entry.ddm_date < _dt.date.today()
            recap_details.clear()
            with recap_details:
                _kv("EAN", entry.ean_colis)
                _kv("Lot", entry.lot_str)
                # DDM en rouge gras si dépassée pour attirer l'œil immédiatement
                if ddm_passed:
                    _kv_red("DDM", entry.ddm_date.strftime("%d/%m/%Y") + "  ⚠ DÉPASSÉE")
                else:
                    _kv("DDM", entry.ddm_date.strftime("%d/%m/%Y"))
                _kv("Format", f"{entry.fmt} ({entry.pcb} btl/carton)")
            recap_details.set_visibility(True)
            # Bandeau d'alerte DDM dès le scan (plus tôt = moins de saisie inutile)
            if ddm_passed:
                ddm_warning_label.text = (
                    f"DDM dépassée le {entry.ddm_date.strftime('%d/%m/%Y')} — "
                    "ne pas imprimer cette étiquette. Scanne un carton plus récent."
                )
                ddm_warning.set_visibility(True)
            else:
                ddm_warning.set_visibility(False)
            layout = get_palette_layout(entry.fmt, entry.product_label)
            layers_input.props(f"max={layout['layers']}")
            extras_input.props(f"max={max(0, layout['per_layer'] - 1)}")
            layers_label.text = (
                f"Tape sur l'étage le plus haut qui est complet "
                f"(max {layout['layers']}) :"
            )
            extras_label.text = (
                "Caisses sur le dessus (étage incomplet, "
                f"max {layout['per_layer'] - 1}) :"
            )
            _rebuild_layers_diagram(layout)
        else:
            recap_image.set_visibility(False)
            recap_label.text = (
                "Scanne un carton ou saisis l'EAN pour identifier le produit."
            )
            recap_label.style(f"color: {COLORS['ink2']}; font-weight: 400; font-size: 14px")
            recap_details.set_visibility(False)
            ddm_warning.set_visibility(False)
            # Pas d'entry → pas de diagramme palette
            layer_buttons.clear()
            layers_diagram.clear()
            extras_max_label.text = ""
        # Wizard : produit visible si entry, qty visible si entry + DDM OK.
        # generate_section est piloté par _refresh_total selon la quantité.
        if entry is not None:
            produit_section.set_visibility(True)
            qty_section.set_visibility(entry.ddm_date >= _dt.date.today())
        else:
            produit_section.set_visibility(False)
            qty_section.set_visibility(False)
            generate_section.set_visibility(False)
        _refresh_total()

    def _refresh_total():
        entry: LabelEntry | None = state["entry"]
        if not entry:
            total_display.text = "—"
            total_display.style(f"color: {COLORS['ink2']}")
            total_capacity_label.text = ""
            generate_btn.disable()
            return
        layout = get_palette_layout(entry.fmt, entry.product_label)
        max_total = layout.get("total") or 0
        if state["full_pallet"] is None:
            total_display.text = "—"
            total_display.style(f"color: {COLORS['ink2']}")
            total_capacity_label.text = "Choisis 'palette pleine' ou 'palette partielle' ci-dessus"
            generate_btn.disable()
            return
        try:
            count = compute_case_count(
                entry.fmt,
                full_pallet=bool(state["full_pallet"]),
                layers_full=int(layers_input.value or 0),
                extras_top=int(extras_input.value or 0),
                product_label=entry.product_label,
            )
        except ValueError as exc:
            total_display.text = "⚠"
            total_display.style(f"color: {COLORS['orange']}; font-weight: 700; font-size: 56px")
            total_capacity_label.text = str(exc)
            generate_btn.disable()
            return
        total_display.text = str(count)
        # Couleur du total :
        #   - vert : tout va bien
        #   - orange : surcharge (count > capacité nominale) → autorisé mais
        #     l'opérateur voit que c'est inhabituel
        if max_total > 0 and count > max_total:
            total_display.style(
                f"color: {COLORS['orange']}; font-weight: 700; "
                "font-size: 56px; line-height: 1.1; text-align: center",
            )
            total_capacity_label.text = (
                f"⚠ surcharge — capacité nominale {max_total} "
                f"(+{count - max_total} sur le dessus)"
            )
        else:
            total_display.style(
                f"color: {COLORS['green']}; font-weight: 700; "
                "font-size: 56px; line-height: 1.1; text-align: center",
            )
            if max_total > 0:
                pct = round(100 * count / max_total)
                total_capacity_label.text = f"sur {max_total} max ({pct}% de la palette)"
            else:
                total_capacity_label.text = ""
        # DDM dépassée → on bloque la génération même si la quantité est valide.
        # Le bandeau d'avertissement dans la card récap explique pourquoi.
        ready = count > 0 and entry.ddm_date >= _dt.date.today()
        if ready:
            generate_btn.enable()
        else:
            generate_btn.disable()
        # Wizard : révéler l'étape 4 (Imprimer) une fois la quantité valide.
        # Si c'est la première fois (passage de hidden → visible), on
        # scrolle vers le bouton pour montrer ce qu'il reste à faire.
        was_hidden = not generate_section.visible
        generate_section.set_visibility(ready)
        if ready and was_hidden:
            ui.run_javascript(
                "setTimeout(() => {"
                "const el = document.getElementById('step-generate');"
                "if (el) el.scrollIntoView({behavior:'smooth', block:'center'});"
                "}, 100);",
            )

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

    def _set_pallet_type_buttons(active: bool | None):
        """Met à jour visuellement les boutons palette pleine/partielle."""
        if active is True:
            full_pallet_btn.props(remove="outline")
            full_pallet_btn.props("unelevated color=green-8")
            partial_pallet_btn.props(remove="unelevated")
            partial_pallet_btn.props("outline color=orange-8")
        elif active is False:
            partial_pallet_btn.props(remove="outline")
            partial_pallet_btn.props("unelevated color=orange-8")
            full_pallet_btn.props(remove="unelevated")
            full_pallet_btn.props("outline color=green-8")
        else:
            full_pallet_btn.props(remove="unelevated")
            full_pallet_btn.props("outline color=green-8")
            partial_pallet_btn.props(remove="unelevated")
            partial_pallet_btn.props("outline color=orange-8")

    def _on_full_pallet_click():
        state["full_pallet"] = True
        _set_pallet_type_buttons(True)
        # En mode pleine : on cache le diagramme (inutile, palette = nominal)
        # mais on affiche le bloc extras pour la surcharge "palette pleine + N".
        partial_container.set_visibility(True)
        layers_label.visible = False
        layers_diagram.visible = False
        no_layer_btn.visible = False
        # Reset layers à 0 — c'est layout["total"] qui pilote le total en mode pleine
        layers_input.value = 0
        _refresh_layers_visual()
        _refresh_total()

    def _on_partial_pallet_click():
        state["full_pallet"] = False
        _set_pallet_type_buttons(False)
        partial_container.set_visibility(True)
        layers_label.visible = True
        layers_diagram.visible = True
        no_layer_btn.visible = True
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
        # Cacher le bouton « Scanner le suivant » : un nouveau scan vient
        # d'arriver, on est reparti pour un cycle complet.
        try:
            next_scan_row.set_visibility(False)
        except (NameError, AttributeError):
            pass
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
        _refresh_recap()  # gère la reveal des sections via la logique centrale
        # Scroll vers la section produit pour que l'opérateur voie le récap
        # (utile surtout sur le chemin scan : sur cascade manuelle, l'opérateur
        # est déjà dans le bas de page, le scroll vers le haut est cohérent).
        ui.run_javascript(
            "setTimeout(() => {"
            "const el = document.getElementById('step-produit');"
            "if (el) el.scrollIntoView({behavior:'smooth', block:'start'});"
            "}, 100);",
        )

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
                matched = dataclasses.replace(
                    matched,
                    lot_str=scan_lot or matched.lot_str,
                    ddm_date=scan_ddm or matched.ddm_date,
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
    full_pallet_btn.on_click(lambda _e: _on_full_pallet_click())
    partial_pallet_btn.on_click(lambda _e: _on_partial_pallet_click())
    no_layer_btn.on_click(lambda _e: _set_layers_full(0))
    extras_minus_btn.on_click(lambda _e: _on_extras_minus())
    extras_plus_btn.on_click(lambda _e: _on_extras_plus())
    # On garde les listeners sur les inputs cachés au cas où une saisie au
    # clavier passerait par eux (defense in depth — pas de chemin actif aujourd'hui).
    layers_input.on_value_change(_on_layers_change)
    extras_input.on_value_change(_on_extras_change)

    async def _do_generate(entry: LabelEntry, count: int):
        """Effectue la génération PDF + sauvegarde historique. Appelé après
        validation par la modale de confirmation."""
        generate_btn.disable()
        generate_btn.props("loading")
        try:
            from common.etiquette_palette_pdf import build_etiquette_palette_pdf

            ctx = _ctx_from_entry(
                entry, count,
                full_pallet=bool(state["full_pallet"]),
                n_copies=2 if double_copies_toggle.value else 1,
                tenant_name=tenant_name,
            )
            pdf_bytes = await asyncio.to_thread(build_etiquette_palette_pdf, ctx)
            safe_gout = re.sub(r"[^A-Za-z0-9_.-]", "_", entry.gout or "")
            fname = (
                f"etiquette_{entry.marque}_{entry.fmt}_"
                f"{safe_gout}_{entry.lot_str}_{count}c.pdf"
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
                full_pallet=bool(state["full_pallet"]),
                n_copies=ctx.n_copies,
                pcb=entry.pcb,
                gtin_uvc=entry.ean_uvc,
                code_interne=entry.code_interne,
                bio=True,
            )
            await asyncio.to_thread(purge_old_label_history, tenant_id)
            _refresh_history()
            next_scan_row.set_visibility(True)
            ui.notify(
                "✓ Étiquette générée — imprime via AirPrint, "
                "puis scanne le carton suivant.",
                type="positive",
                icon="check",
                timeout=5000,
            )
        except Exception as exc:
            _log.exception("Erreur génération PDF étiquette palette")
            ui.notify(f"Erreur génération PDF : {exc}", type="negative")
        finally:
            generate_btn.enable()
            generate_btn.props(remove="loading")

    async def _on_generate():
        entry: LabelEntry | None = state["entry"]
        if not entry:
            ui.notify("Sélectionne marque, bouteille et goût.", type="warning")
            return
        if state["full_pallet"] is None:
            ui.notify(
                "Choisis 'Palette pleine' ou 'Palette partielle' avant d'imprimer.",
                type="warning",
            )
            return
        try:
            count = compute_case_count(
                entry.fmt,
                full_pallet=bool(state["full_pallet"]),
                layers_full=int(layers_input.value or 0),
                extras_top=int(extras_input.value or 0),
                product_label=entry.product_label,
            )
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return

        # Validations métier
        if count <= 0:
            ui.notify(
                "La quantité doit être > 0 pour générer une étiquette.",
                type="warning",
            )
            return
        if count > 999:
            ui.notify(
                f"Quantité {count} > 999 (limite encodage GS1-128 AI 37). "
                "Vérifie ta saisie.",
                type="negative",
            )
            return
        if entry.ddm_date < _dt.date.today():
            ui.notify(
                f"⚠ DDM dépassée ({entry.ddm_date.strftime('%d/%m/%Y')}) — "
                "scan une étiquette plus récente ou saisis manuellement.",
                type="warning",
                timeout=6000,
            )
            return

        # Modale de confirmation : dernier garde-fou humain avant impression.
        # Pour une étiquette qui finit collée sur une palette livrée client,
        # on veut une lecture explicite du chiffre principal et des metadatas.
        _open_confirm_dialog(entry, count)

    def _open_confirm_dialog(entry: LabelEntry, count: int):
        n_copies = 2 if double_copies_toggle.value else 1
        full = bool(state["full_pallet"])
        layout = get_palette_layout(entry.fmt, entry.product_label)
        max_total = int(layout.get("total") or 0)
        is_overload = max_total > 0 and count > max_total
        with ui.dialog() as confirm_dlg, ui.card().classes("q-pa-lg").style(
            "min-width: 360px; max-width: 420px",
        ):
            ui.label("Vérifie avant d'imprimer").classes("text-h6").style(
                f"color: {COLORS['ink']}; font-weight: 700",
            )
            ui.separator().classes("q-my-sm")

            # Le chiffre clé : énorme, centré, vert (orange si surcharge)
            number_color = COLORS['orange'] if is_overload else COLORS['green']
            with ui.column().classes("w-full items-center gap-0 q-mb-sm"):
                ui.label(str(count)).style(
                    f"color: {number_color}; font-weight: 800; "
                    "font-size: 72px; line-height: 1",
                )
                ui.label("CARTONS").classes("text-caption").style(
                    f"color: {COLORS['ink2']}; letter-spacing: 2px; font-weight: 600",
                )
                if is_overload:
                    over = count - max_total
                    type_label = (
                        f"Palette pleine + {over} en surcharge"
                        if full else f"Palette partielle ({over} de plus que la nominale)"
                    )
                else:
                    type_label = "Palette pleine" if full else "Palette partielle"
                ui.label(type_label).classes("text-body2 q-mt-xs").style(
                    f"color: {number_color}; font-weight: 600",
                )

            ui.separator().classes("q-my-sm")

            # Détail produit
            with ui.column().classes("w-full gap-1"):
                ui.label(entry.designation).classes("text-body1").style(
                    f"color: {COLORS['ink']}; font-weight: 600",
                )
                ui.label(
                    f"Lot {entry.lot_str} — DDM {entry.ddm_date.strftime('%d/%m/%Y')}",
                ).classes("text-body2").style(f"color: {COLORS['ink2']}")
                ui.label(
                    f"{n_copies} étiquette" + ("s (2 faces de palette)" if n_copies > 1 else ""),
                ).classes("text-caption").style(f"color: {COLORS['ink2']}")

            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Annuler", on_click=confirm_dlg.close).props(
                    "flat color=grey-7",
                )
                async def _confirmed():
                    confirm_dlg.close()
                    await _do_generate(entry, count)
                ui.button(
                    "✓ Imprimer", icon="print", on_click=_confirmed,
                ).props("color=green-8 unelevated size=lg")
        confirm_dlg.open()

    generate_btn.on_click(_on_generate)

    def _reset_for_next_scan():
        """Reset le formulaire pour scanner un nouveau carton, en gardant les
        préférences (palette pleine/partielle, 2 exemplaires) qui sont stables
        sur une série de palettes du même produit."""
        state["marque"] = None
        state["bottle"] = None
        state["gout"] = None
        state["entry"] = None
        _set_active_button(marque_buttons, None)
        for btn in bottle_buttons.values():
            btn.disable()
        _set_active_button(bottle_buttons, None)
        gout_select.options = []
        gout_select.value = None
        gout_select.disable()
        layers_input.value = 0
        extras_input.value = 0
        _refresh_layers_visual()
        _refresh_extras_visual()
        # On préserve state["full_pallet"] : si l'opérateur étiquette une série
        # de palettes pleines du même produit, il ne devrait pas avoir à
        # rechoisir à chaque fois. Visuel des boutons cohérent.
        partial_container.set_visibility(state["full_pallet"] is False)
        _set_pallet_type_buttons(state["full_pallet"])
        next_scan_row.set_visibility(False)
        _refresh_recap()
        # Wizard : ré-cacher les étapes 2/3/4. Seul step 1 (scan) reste visible.
        produit_section.set_visibility(False)
        qty_section.set_visibility(False)
        generate_section.set_visibility(False)
        # Remonter en haut pour montrer le bouton "Scanner un carton"
        ui.run_javascript("window.scrollTo({top: 0, behavior: 'smooth'})")

    # ────────────────────────────────────────────────────────────────────
    # Étiquettes récentes (historique pour réimpression et audit)
    # ────────────────────────────────────────────────────────────────────
    history_section = ui.column().classes("w-full q-mt-lg")

    async def _do_reprint(h: HistoryEntry):
        try:
            from common.etiquette_palette_pdf import build_etiquette_palette_pdf
            ctx = _ctx_from_history(h, tenant_name=tenant_name)
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
        with history_section:
            _render_history_card(recent, on_reprint=_do_reprint)

    _refresh_history()


# ─── Saisie manuelle EAN (fallback si scan échoue) ──────────────────────────

def _open_manual_ean_dialog(handler) -> None:
    """Petit dialog pour saisie manuelle d'un EAN, avec callback handler(ean).

    Utilisé quand le scan ne donne rien ou que l'EAN n'est pas dans la sync.
    Le ``handler`` peut être sync ou async — on await dans le second cas.
    """
    import inspect

    with ui.dialog() as dlg, ui.card().classes("q-pa-md").style("min-width: 320px"):
        ui.label("Saisie manuelle EAN").classes("text-subtitle1")
        ean_input = ui.input(
            placeholder="ex: 3770014427250",
        ).classes("w-full").props("outlined dense autofocus")

        async def _submit():
            val = (ean_input.value or "").strip()
            if not val:
                return
            dlg.close()
            res = handler(val)
            if inspect.isawaitable(res):
                await res

        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Annuler", on_click=dlg.close).props("flat color=grey-7")
            ui.button(
                "Valider", icon="check",
                on_click=_submit,
            ).props("color=green-8 unelevated")
        ean_input.on("keydown.enter", _submit)
    dlg.open()


# ─── Helpers ────────────────────────────────────────────────────────────────

_SCAN_INPUT_LISTENER_JS = """
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

    // Feedback perceptif scan réussi : vibration courte (iPhone/iPad) + bip
    // 880 Hz 180 ms. Le clic photo précédent compte comme user gesture, donc
    // l'AudioContext est autorisé. Échec silencieux si l'API manque.
    function _fsScanFeedback(success) {
        try {
            if (navigator.vibrate) {
                navigator.vibrate(success ? [60, 30, 60] : [200]);
            }
        } catch (e) { /* noop */ }
        try {
            const AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return;
            const ctx = new AC();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.frequency.value = success ? 880 : 220;
            osc.type = 'sine';
            gain.gain.setValueAtTime(0.18, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(
                0.001, ctx.currentTime + 0.18,
            );
            osc.connect(gain).connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.2);
            // Libère le contexte audio après la note pour ne pas accumuler.
            setTimeout(() => { try { ctx.close(); } catch (e) {} }, 300);
        } catch (e) { /* noop */ }
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
                    _fsScanFeedback(true);
                    emitEvent('barcode_scanned', data);
                } else {
                    _fsScanFeedback(false);
                    emitEvent('barcode_error',
                        (data.error || 'Aucun code-barres détecté'));
                }
            } catch (err) {
                _fsScanFeedback(false);
                emitEvent('barcode_error', String(err));
            }
            e.target.value = '';
        });
    };
    wait();
})();
</script>
"""


def _render_history_card(
    entries: list[HistoryEntry],
    *,
    on_reprint,
) -> None:
    """Rend la card « Étiquettes récentes » à partir d'une liste d'entries.

    Doit être appelé dans un contexte UI (entre ``with section:`` du caller).
    Si ``entries`` est vide, ne rend rien (silencieux).
    """
    if not entries:
        return
    section_title("Étiquettes récentes", "history")
    with ui.card().classes("w-full q-pa-none").props("flat bordered"):
        for i, h in enumerate(entries):
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
                        on_click=lambda _e, hh=h: on_reprint(hh),
                    ).props("flat color=green-8 dense")


def _install_scan_input_listener() -> None:
    """Injecte le JS qui écoute le file input + upload + emitEvent vers Python.

    Idempotent : ``window._fsScanInputBound`` empêche le double-binding si la
    page est ré-ouverte sans full reload (édge case Quasar/NiceGUI).
    """
    ui.add_body_html(_SCAN_INPUT_LISTENER_JS)


def _ctx_from_entry(
    entry: LabelEntry, count: int,
    *, full_pallet: bool, n_copies: int, tenant_name: str,
):
    """Construit un EtiquetteContext depuis une LabelEntry + saisie quantité."""
    from common.etiquette_palette_pdf import EtiquetteContext
    return EtiquetteContext(
        product_label=entry.product_label,
        fmt=entry.fmt,
        ean13=entry.ean_colis,
        lot=entry.lot_str,
        ddm=entry.ddm_date,
        case_count=count,
        full_pallet=full_pallet,
        tenant_name=tenant_name,
        n_copies=n_copies,
        marque=entry.marque,
        code_interne=entry.code_interne,
        gtin_uvc=entry.ean_uvc,
        pcb=entry.pcb,
        bio=True,
    )


def _ctx_from_history(h: HistoryEntry, *, tenant_name: str):
    """Construit un EtiquetteContext depuis une HistoryEntry (réimpression)."""
    from common.etiquette_palette_pdf import EtiquetteContext
    return EtiquetteContext(
        product_label=h.designation or f"GTIN {h.ean}",
        fmt=h.fmt,
        ean13=h.ean,
        lot=h.lot,
        ddm=h.ddm,
        case_count=h.case_count,
        full_pallet=h.full_pallet,
        tenant_name=tenant_name,
        n_copies=h.n_copies,
        marque=h.marque,
        code_interne=h.code_interne,
        gtin_uvc=h.gtin_uvc,
        pcb=h.pcb,
        bio=h.bio,
    )


def _kv(label: str, value: str) -> None:
    """Affiche une ligne 'label : value' dans le récap."""
    with ui.row().classes("w-full gap-2"):
        ui.label(f"{label} :").classes("text-body2").style(
            f"color: {COLORS['ink2']}; min-width: 140px",
        )
        ui.label(value).classes("text-body2").style(
            f"color: {COLORS['ink']}; font-weight: 500",
        )


def _kv_red(label: str, value: str) -> None:
    """Variante de _kv avec valeur en rouge gras (DDM dépassée, etc.)."""
    with ui.row().classes("w-full gap-2"):
        ui.label(f"{label} :").classes("text-body2").style(
            f"color: {COLORS['ink2']}; min-width: 140px",
        )
        ui.label(value).classes("text-body2").style(
            "color:#B91C1C; font-weight: 700",
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
