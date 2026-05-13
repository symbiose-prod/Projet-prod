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
     choisit ensuite palette pleine/partielle, ajuste les caisses via le
     diagramme cliquable + compteur +/-, puis génère le PDF.

Fallback : si le scan échoue, l'opérateur peut saisir l'EAN à la main —
le lookup interroge la matrice codes-barres EasyBeer fraîche (cache
24 h) puis tombe sur la sync étiquettes (``sync_operations``) si
EasyBeer n'a pas le produit.

UI en wizard 4 étapes (révélation progressive) : [1] Scan, [2] Produit
identifié, [3] Quantité, [4] Imprimer.

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
from common.services.print_jobs_service import (
    PendingJobView,
    list_pending_jobs,
)
from pages.auth import require_auth
from pages.theme import COLORS, page_layout

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
            "imprime sur la Brother (par défaut) ou via AirPrint."
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
    """Rend le formulaire wizard 4 étapes : scan/EAN manuel → produit
    identifié → quantité (palette pleine/partielle + diagramme) → imprimer."""
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
        """Saisie manuelle EAN — supporte 3 formats d'entrée :

        1. ``23770014427049`` (14 digits) : GTIN seul. Lot/DDM resteront
           vides (le système met des fallbacks).
        2. ``01237700144270491527050810080527`` (digits) : payload GS1-128
           complet. On extrait GTIN (AI 01), DDM (AI 15) et Lot (AI 10).
        3. ``(01)23770014427049(15)270508(10)080527`` : format avec
           parenthèses (HRI lisible humain).
        """
        from common.services.etiquette_palette_service import (
            parse_gs1_ddm,
            parse_gs1_digits,
            parse_gs1_string,
        )

        cleaned = (ean or "").strip()
        if not cleaned:
            return

        # Détection du format et extraction des AIs
        extracted_gtin = cleaned
        extracted_lot = ""
        extracted_ddm: _dt.date | None = None

        if "(" in cleaned:
            # Format avec parenthèses (HRI)
            ais = parse_gs1_string(cleaned)
        else:
            # Format digits — n'essayer le parser GS1 que si la chaîne fait
            # plus de 16 digits et commence par un AI connu (01 ou 02).
            # Sinon (14 digits par ex), on garde tel quel comme GTIN.
            digits = re.sub(r"\D+", "", cleaned)
            if len(digits) > 16 and digits.startswith(("01", "02")):
                ais = parse_gs1_digits(digits)
            else:
                ais = {}

        if ais:
            gtin = ais.get("01") or ais.get("02")
            if gtin:
                extracted_gtin = gtin
                extracted_lot = ais.get("10", "")
                ddm_str = ais.get("15") or ais.get("17") or ""
                if ddm_str:
                    extracted_ddm = parse_gs1_ddm(ddm_str)

        ui.notify("🔍 Recherche du produit…", type="info", timeout=2000)
        try:
            product = await asyncio.to_thread(lookup_product_by_ean, extracted_gtin)
        except Exception:
            _log.exception("Erreur lookup matrice EasyBeer (saisie manuelle)")
            product = None
        _handle_scanned_data({
            "ean": extracted_gtin,
            "lot": extracted_lot,
            "ddm": extracted_ddm.isoformat() if extracted_ddm else None,
            "product": product,
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

        # Cascade fallback (marque/bouteille/goût) supprimée : le flow se
        # repose sur scan + saisie manuelle EAN. Si un opérateur tombe
        # sur un carton illisible et un EAN inconnu, il prend un autre
        # carton de la même palette.

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
                layers_label = ui.label("Indique le nombre d'étage complet :").classes(
                    "text-body2",
                ).style(f"color: {COLORS['ink']}; font-weight: 500")

                # Diagramme palette : empilement de boutons-étages cliquables.
                # Reconstruit dynamiquement à chaque scan (le format change).
                layers_diagram = ui.column().classes("w-full gap-1").style("max-width: 360px")
                no_layer_btn = ui.button(
                    "Aucun étage rempli", icon="block",
                ).classes("w-full q-mt-xs").props(
                    "outline color=grey-8 size=md",
                ).style("min-height: 44px; touch-action: manipulation")

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

        with ui.column().classes("w-full gap-2"):
            # Bouton primaire — impression directe via l'agent Brother
            print_direct_btn = ui.button(
                "Imprimer directement",
                icon="print",
            ).classes("w-full").props(
                "color=green-8 unelevated size=lg",
            ).style(
                "touch-action: manipulation; min-height: 56px; "
                "font-size: 16px; font-weight: 600",
            )
            print_direct_btn.disable()

            # Bouton secondaire — fallback PDF (AirPrint, ou si l'agent est down)
            generate_btn = ui.button(
                "Télécharger le PDF",
                icon="picture_as_pdf",
            ).classes("w-full").props(
                "color=grey-7 outline size=md",
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

    def _set_print_buttons(enabled: bool, loading: bool = False) -> None:
        """Active/désactive les 2 boutons d'impression simultanément.

        Évite que l'opérateur tape sur "Télécharger PDF" pendant qu'une
        impression directe est en cours, et inversement.
        """
        if enabled:
            print_direct_btn.enable()
            generate_btn.enable()
        else:
            print_direct_btn.disable()
            generate_btn.disable()
        if loading:
            print_direct_btn.props("loading")
            generate_btn.props("loading")
        else:
            print_direct_btn.props(remove="loading")
            generate_btn.props(remove="loading")

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

    def _refresh_recap():
        """Met à jour la card récap depuis state["entry"] (posée par scan ou
        saisie manuelle EAN). La cascade marque/bouteille/goût n'existe plus,
        donc l'unique source d'entry est le chemin scan/EAN."""
        entry: LabelEntry | None = state.get("entry")

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
                f"Indique le nombre d'étage complet (max {layout['layers']}) :"
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
            _set_print_buttons(False)
            return
        layout = get_palette_layout(entry.fmt, entry.product_label)
        max_total = layout.get("total") or 0
        if state["full_pallet"] is None:
            total_display.text = "—"
            total_display.style(f"color: {COLORS['ink2']}")
            total_capacity_label.text = "Choisis 'palette pleine' ou 'palette partielle' ci-dessus"
            _set_print_buttons(False)
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
            _set_print_buttons(False)
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
        _set_print_buttons(ready)
        # Wizard : révéler l'étape 4 (Imprimer) une fois la quantité valide.
        # Si c'est la première fois (passage de hidden → visible), on
        # scrolle vers le bouton pour montrer ce qu'il reste à faire.
        was_hidden = not generate_section.visible
        generate_section.set_visibility(ready)
        if ready and was_hidden:
            # block:'nearest' = scroll uniquement si l'étape Imprimer
            # n'est pas déjà visible. Évite de masquer les +/- de l'étape
            # 3 (qty) en sur-scrollant comme c'était le cas avec 'center'.
            ui.run_javascript(
                "setTimeout(() => {"
                "const el = document.getElementById('step-generate');"
                "if (el) el.scrollIntoView({behavior:'smooth', block:'nearest'});"
                "}, 100);",
            )

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
        _refresh_recap()  # gère la reveal des sections via la logique centrale
        # Scroll vers la section produit pour que l'opérateur voie le récap.
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
                ean_uvc=product.get("ean_uvc") or "",
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

    full_pallet_btn.on_click(lambda _e: _on_full_pallet_click())
    partial_pallet_btn.on_click(lambda _e: _on_partial_pallet_click())
    no_layer_btn.on_click(lambda _e: _set_layers_full(0))
    extras_minus_btn.on_click(lambda _e: _on_extras_minus())
    extras_plus_btn.on_click(lambda _e: _on_extras_plus())
    # On garde les listeners sur les inputs cachés au cas où une saisie au
    # clavier passerait par eux (defense in depth — pas de chemin actif aujourd'hui).
    layers_input.on_value_change(_on_layers_change)
    extras_input.on_value_change(_on_extras_change)

    async def _do_generate(entry: LabelEntry, count: int, *, mode: str = "download"):
        """Génère le PDF + audit + (téléchargement|envoi à l'imprimante).

        ``mode`` :
          - 'download' : ui.download(pdf) → AirPrint via iOS
          - 'print_direct' : POST /api/print-jobs → agent Brother imprime
        """
        _set_print_buttons(False, loading=True)
        try:
            from common.etiquette_palette_pdf import build_etiquette_palette_pdf
            from common.services.sscc_service import generate_sscc

            # Génération du SSCC palette (NEW). Atomic via PG sequence,
            # jamais réutilisé, audité dans sscc_log. Si la DB échoue on
            # log mais on continue : la palette aura un SSCC vide plutôt
            # qu'aucune étiquette.
            try:
                sscc_result = await asyncio.to_thread(
                    generate_sscc,
                    tenant_id,
                    user_email=user_email,
                    gtin_palette=entry.ean_colis,
                    lot=entry.lot_str,
                    ddm=entry.ddm_date,
                    case_count=count,
                )
                sscc_str = sscc_result.sscc
            except Exception:
                _log.exception("Échec génération SSCC — étiquette imprimée sans SSCC")
                sscc_str = ""

            ctx = _ctx_from_entry(
                entry, count,
                full_pallet=bool(state["full_pallet"]),
                n_copies=2 if double_copies_toggle.value else 1,
                tenant_name=tenant_name,
                sscc=sscc_str,
            )
            pdf_bytes = await asyncio.to_thread(build_etiquette_palette_pdf, ctx)
            safe_gout = re.sub(r"[^A-Za-z0-9_.-]", "_", entry.gout or "")
            fname = (
                f"etiquette_{entry.marque}_{entry.fmt}_"
                f"{safe_gout}_{entry.lot_str}_{count}c.pdf"
            )

            if mode == "print_direct":
                # Soumet à la queue d'impression — l'agent Windows long-poll
                # /api/print-jobs/next, récupère le PDF et imprime via le
                # driver Brother (Windows ShellExecute).
                await _submit_print_job(pdf_bytes, fname, ctx.n_copies)
                ui.notify(
                    f"✓ Envoyé à l'imprimante Brother — {count} caisses, "
                    f"{ctx.n_copies} étiquette(s).",
                    type="positive", icon="print", timeout=5000,
                )
            else:
                ui.download(pdf_bytes, fname)
                ui.notify(
                    "✓ PDF téléchargé — imprime via AirPrint puis scanne le suivant.",
                    type="positive", icon="check", timeout=5000,
                )

            # Audit historique (fire-and-forget) — identique pour les 2 modes.
            # On stocke le SSCC pour qu'une réimpression future utilise le
            # même (même palette physique).
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
                sscc=sscc_str,
            )
            await asyncio.to_thread(purge_old_label_history, tenant_id)
            _refresh_history()
            # Rafraîchir le mini-menu sidebar (pending + récentes)
            refresh_sidebar = state.get("_refresh_sidebar")
            if refresh_sidebar:
                refresh_sidebar()
            next_scan_row.set_visibility(True)
        except Exception as exc:
            _log.exception("Erreur génération étiquette palette (mode=%s)", mode)
            ui.notify(f"Erreur : {exc}", type="negative")
        finally:
            _set_print_buttons(True, loading=False)

    async def _submit_print_job(pdf_bytes: bytes, filename: str, n_copies: int) -> None:
        """Crée un job d'impression en queue et signale l'agent.

        On appelle le service domaine directement (pas via HTTP) puisque
        nous sommes côté serveur — économise un round-trip et évite la
        gymnastique d'auth session. Le signal in-process réveille
        immédiatement le long-poll de l'agent.
        """
        from common.services.print_jobs_service import create_print_job
        job_id = await asyncio.to_thread(
            create_print_job,
            tenant_id,
            user_email=user_email,
            pdf_bytes=pdf_bytes,
            filename=filename,
            n_copies=n_copies,
        )
        if not job_id:
            raise RuntimeError("Impossible de créer le job d'impression")
        # Réveil de l'agent en attente sur /api/print-jobs/next.
        # Import lazy : app_nicegui importe pages.etiquettes_palette au
        # démarrage, donc on doit éviter un import top-level circulaire.
        try:
            from app_nicegui import _signal_new_print_job
            _signal_new_print_job(tenant_id)
        except Exception:
            _log.warning("Impossible de signaler la queue d'impression", exc_info=True)

    def _validate_before_generate() -> tuple[LabelEntry, int] | None:
        """Vérifie que tout est OK avant d'ouvrir la modale. Retourne
        (entry, count) si OK, None sinon (notify déjà émis)."""
        entry: LabelEntry | None = state["entry"]
        if not entry:
            ui.notify("Scanne ou saisis l'EAN d'abord.", type="warning")
            return None
        if state["full_pallet"] is None:
            ui.notify(
                "Choisis 'Palette pleine' ou 'Palette partielle' avant d'imprimer.",
                type="warning",
            )
            return None
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
            return None
        if count <= 0:
            ui.notify("La quantité doit être > 0.", type="warning")
            return None
        if count > 999:
            ui.notify(
                f"Quantité {count} > 999 (limite encodage GS1-128 AI 37).",
                type="negative",
            )
            return None
        if entry.ddm_date < _dt.date.today():
            ui.notify(
                f"⚠ DDM dépassée ({entry.ddm_date.strftime('%d/%m/%Y')}) — "
                "scanne un carton plus récent.",
                type="warning", timeout=6000,
            )
            return None
        return entry, count

    async def _on_print_direct():
        """Bouton 'Imprimer directement' → modale → agent Brother."""
        validated = _validate_before_generate()
        if validated is None:
            return
        entry, count = validated
        _open_confirm_dialog(entry, count, mode="print_direct")

    async def _on_generate():
        """Bouton 'Télécharger PDF' → modale → ui.download → AirPrint."""
        validated = _validate_before_generate()
        if validated is None:
            return
        entry, count = validated
        _open_confirm_dialog(entry, count, mode="download")

    def _open_confirm_dialog(entry: LabelEntry, count: int, *, mode: str):
        n_copies = 2 if double_copies_toggle.value else 1
        full = bool(state["full_pallet"])
        layout = get_palette_layout(entry.fmt, entry.product_label)
        max_total = int(layout.get("total") or 0)
        is_overload = max_total > 0 and count > max_total
        is_direct = mode == "print_direct"
        with ui.dialog() as confirm_dlg, ui.card().classes("q-pa-lg").style(
            "min-width: 360px; max-width: 420px",
        ):
            title = "Imprimer sur la Brother ?" if is_direct else "Télécharger le PDF ?"
            ui.label(title).classes("text-h6").style(
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
                    await _do_generate(entry, count, mode=mode)
                action_label = "✓ Imprimer" if is_direct else "✓ Télécharger"
                action_icon = "print" if is_direct else "download"
                ui.button(
                    action_label, icon=action_icon, on_click=_confirmed,
                ).props("color=green-8 unelevated size=lg")
        confirm_dlg.open()

    generate_btn.on_click(_on_generate)
    print_direct_btn.on_click(_on_print_direct)

    def _reset_for_next_scan():
        """Reset le formulaire pour scanner un nouveau carton, en gardant les
        préférences (palette pleine/partielle, 2 exemplaires) qui sont stables
        sur une série de palettes du même produit."""
        state["marque"] = None
        state["bottle"] = None
        state["gout"] = None
        state["entry"] = None
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
        if h.voided_at:
            ui.notify(
                "Étiquette annulée — réimpression refusée.",
                type="warning", icon="block",
            )
            return
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

    def _do_void(h: HistoryEntry):
        """Ouvre un dialog pour annuler un SSCC (étiquette fantôme)."""
        if not h.sscc:
            ui.notify(
                "Cette ancienne entrée n'a pas de SSCC — rien à annuler.",
                type="warning",
            )
            return
        if h.voided_at:
            ui.notify("Déjà annulée.", type="info")
            return
        _open_void_dialog(
            sscc=h.sscc, designation=f"{h.designation} {h.fmt}",
            on_confirmed=lambda reason: _confirm_void(h.sscc, reason),
        )

    async def _confirm_void(sscc: str, reason: str):
        from common.services.sscc_service import void_sscc
        ok = await asyncio.to_thread(
            void_sscc, tenant_id, sscc, reason=reason, user_email=user_email,
        )
        if ok:
            ui.notify(
                f"✓ Palette {_fmt_sscc_short(sscc)} annulée — "
                "elle n'apparaîtra plus dans le chargement.",
                type="positive", icon="block", timeout=4000,
            )
            _refresh_history()
            refresh_sidebar = state.get("_refresh_sidebar")
            if refresh_sidebar:
                refresh_sidebar()
        else:
            ui.notify(
                "Annulation impossible (SSCC déjà annulé ou introuvable).",
                type="warning",
            )

    def _refresh_history():
        """Recharge la section historique (appelée après chaque génération).

        Format : expansion repliable contenant un tableau condensé des
        20 dernières étiquettes générées. L'opérateur peut l'ouvrir pour
        retrouver une étiquette à réimprimer ou annuler une fantôme.
        """
        history_section.clear()
        if not tenant_id:
            return
        recent = list_recent_labels(tenant_id, limit=20)
        with history_section:
            _render_history_table(recent, on_reprint=_do_reprint, on_void=_do_void)

    _refresh_history()

    # ────────────────────────────────────────────────────────────────────
    # Mini-menu compact en bas de page : "À imprimer" + "3 dernières"
    # ────────────────────────────────────────────────────────────────────
    mini_menu_container = ui.column().classes("w-full q-mt-md").style(
        f"border-top: 1px solid {COLORS['border']}; padding-top: 12px",
    )

    def _refresh_sidebar():
        """Reconstruit le mini-menu (À imprimer + 3 dernières). Le nom est
        historique — la widget vit maintenant en bas de page, pas dans la
        sidebar, mais l'API publique reste la même."""
        if not tenant_id:
            return
        mini_menu_container.clear()
        try:
            pending = list_pending_jobs(tenant_id, limit=8)
        except Exception:
            pending = []
            _log.warning("Mini-menu : list_pending_jobs échec", exc_info=True)
        try:
            recent = list_recent_labels(tenant_id, limit=3)
        except Exception:
            recent = []
            _log.warning("Mini-menu : list_recent_labels échec", exc_info=True)

        with mini_menu_container:
            _render_sidebar_widget(pending, recent, on_refresh=_refresh_sidebar)

    _refresh_sidebar()
    # Mémorise dans l'état pour pouvoir le rafraîchir après chaque
    # soumission de print job (capture par closure).
    state["_refresh_sidebar"] = _refresh_sidebar


# ─── Sidebar widget compact ─────────────────────────────────────────────────

_FNAME_RE = re.compile(
    r"^etiquette(?:_REIMPR)?_([^_]+)_([^_]+)_(.+?)_([^_]+)_(\d+)c\.pdf$",
)


def _parse_label_filename(fn: str) -> dict:
    """Parse 'etiquette_NIKO_12x33_Mangue_Passion_08052027_126c.pdf' → dict.

    Retourne {"marque", "fmt", "gout", "lot", "count"} ou {"raw": fn}
    si le format est inconnu.
    """
    m = _FNAME_RE.match(fn or "")
    if not m:
        return {"raw": fn}
    return {
        "marque": m.group(1),
        "fmt": m.group(2),
        "gout": m.group(3).replace("_", " "),
        "lot": m.group(4),
        "count": int(m.group(5)),
    }


def _format_short_time(dt) -> str:
    """Formate un datetime en HH:MM heure de Paris.

    Les DB renvoient des datetimes en UTC ; sans conversion on aurait 2h
    de décalage en été (CEST). fmt_paris() force la conversion.
    """
    from common.ramasse import fmt_paris
    s = fmt_paris(dt, "%H:%M")
    return s or "?"


def _render_sidebar_widget(
    pending: list[PendingJobView],
    recent: list[HistoryEntry],
    on_refresh,
) -> None:
    """Rend le mini-menu sidebar :
      - Section "À imprimer" : jobs en queue (pending + printing)
      - Section "Récentes" : 3 dernières étiquettes générées
      - Bouton refresh discret en bas
    Appelée dans un contexte UI (entre with sidebar:)."""

    # ── Section : à imprimer ──
    with ui.row().classes("w-full items-center gap-1 q-mt-sm"):
        ui.icon("print", size="xs").style(f"color: {COLORS['orange']}")
        ui.label(f"À IMPRIMER ({len(pending)})").classes("text-overline").style(
            f"color: {COLORS['ink2']}; font-weight: 700; letter-spacing: 1px",
        )

    if not pending:
        ui.label("Aucune en attente").classes("text-caption q-pl-xs").style(
            f"color: {COLORS['ink2']}; font-style: italic",
        )
    else:
        for j in pending:
            parsed = _parse_label_filename(j.filename)
            with ui.column().classes("w-full gap-0 q-pl-xs q-mb-xs"):
                if "marque" in parsed:
                    label = (
                        f"{parsed['marque']} {parsed['fmt']} "
                        f"{parsed['gout']} — {parsed['count']}c"
                    )
                else:
                    label = parsed.get("raw", "?")
                ui.label(label).classes("text-caption").style(
                    f"color: {COLORS['ink']}; font-weight: 500; line-height: 1.2",
                )
                # Sous-titre : status + heure
                status_color = (
                    COLORS["orange"] if j.status == "printing"
                    else COLORS["ink2"]
                )
                ui.label(
                    f"{j.status} · {_format_short_time(j.created_at)}",
                ).classes("text-caption").style(
                    f"color: {status_color}; font-size: 10px; line-height: 1.2",
                )

    # ── Section : 3 dernières ──
    ui.separator().classes("q-my-sm")
    with ui.row().classes("w-full items-center gap-1"):
        ui.icon("history", size="xs").style(f"color: {COLORS['green']}")
        ui.label("RÉCENTES").classes("text-overline").style(
            f"color: {COLORS['ink2']}; font-weight: 700; letter-spacing: 1px",
        )

    if not recent:
        ui.label("Aucune encore").classes("text-caption q-pl-xs").style(
            f"color: {COLORS['ink2']}; font-style: italic",
        )
    else:
        for h in recent[:3]:
            with ui.column().classes("w-full gap-0 q-pl-xs q-mb-xs"):
                short = (
                    f"{h.marque} {h.fmt} "
                    f"{h.gout or '—'} — {h.case_count}c"
                )
                ui.label(short).classes("text-caption").style(
                    f"color: {COLORS['ink']}; font-weight: 500; line-height: 1.2",
                )
                ui.label(
                    _format_short_time(h.generated_at),
                ).classes("text-caption").style(
                    f"color: {COLORS['ink2']}; font-size: 10px; line-height: 1.2",
                )

    # ── Bouton refresh discret ──
    with ui.row().classes("w-full justify-end q-mt-xs"):
        ui.button(
            icon="refresh",
            on_click=lambda: on_refresh(),
        ).props("flat dense color=grey-7 size=sm").style(
            "min-width: 28px; min-height: 28px",
        )


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


def _render_history_table(
    entries: list[HistoryEntry],
    *,
    on_reprint,
    on_void=None,
) -> None:
    """Rend les étiquettes récentes sous forme d'un tableau Quasar
    encapsulé dans une ``ui.expansion`` repliable.

    Chaque ligne propose 2 actions :
      - Réimprimer : regénère le PDF avec le SSCC d'origine
      - Annuler : marque le SSCC comme fantôme (étiquette pas imprimée
        ou doublon). Les lignes annulées apparaissent grisées et leur
        bouton Réimprimer est désactivé.
    """
    label_text = (
        f"Étiquettes récentes ({len(entries)})"
        if entries else "Étiquettes récentes"
    )
    with ui.expansion(
        text=label_text, icon="history",
    ).classes("w-full").props("dense"):
        if not entries:
            ui.label("Aucune étiquette générée pour l'instant.").classes(
                "text-body2 q-pa-md",
            ).style(f"color: {COLORS['ink2']}; font-style: italic")
            return

        # Map id → HistoryEntry pour les callbacks d'action
        by_id: dict[int, HistoryEntry] = {h.id: h for h in entries}

        rows = []
        for h in entries:
            from common.ramasse import fmt_paris as _fmt_paris
            when = _fmt_paris(h.generated_at, "%d/%m %H:%M")
            is_voided = bool(h.voided_at)
            produit_str = f"{h.designation or 'GTIN ' + h.ean} — {h.fmt}"
            if is_voided:
                # Marqueur visuel devant le produit annulé
                produit_str = f"⊘ {produit_str}"
            rows.append({
                "id": h.id,
                "produit": produit_str,
                "lot": h.lot,
                "ddm": h.ddm.strftime("%d/%m/%Y"),
                "cartons": h.case_count,
                "when": when,
                "voided": is_voided,
            })

        cols = [
            {"name": "produit", "label": "Produit", "field": "produit",
             "align": "left", "sortable": True},
            {"name": "lot", "label": "Lot", "field": "lot",
             "align": "left", "sortable": True},
            {"name": "ddm", "label": "DDM", "field": "ddm",
             "align": "left", "sortable": True},
            {"name": "cartons", "label": "Cartons", "field": "cartons",
             "align": "right", "sortable": True},
            {"name": "when", "label": "Imprimée", "field": "when",
             "align": "left", "sortable": True},
            {"name": "id", "label": "", "field": "id", "align": "center"},
        ]

        table = ui.table(
            columns=cols, rows=rows, row_key="id",
            pagination={"rowsPerPage": 10},
        ).classes("w-full").props("flat bordered dense")

        # Slot row : grise les lignes annulées
        table.add_slot("body", """
            <q-tr :props="props" :style="props.row.voided ?
                'opacity: 0.5; text-decoration: line-through' : ''">
                <q-td v-for="col in props.cols" :key="col.name" :props="props">
                    <template v-if="col.name === 'id'">
                        <q-btn flat dense color="green-8" icon="print"
                               :disable="props.row.voided"
                               @click="$parent.$emit('reprint', props.row.id)" />
                        <q-btn flat dense color="red-7" icon="block"
                               :disable="props.row.voided"
                               @click="$parent.$emit('void', props.row.id)" />
                    </template>
                    <template v-else>
                        {{ col.value }}
                    </template>
                </q-td>
            </q-tr>
        """)

        def _on_reprint_event(e):
            try:
                row_id = int(e.args)
            except (TypeError, ValueError):
                return
            h = by_id.get(row_id)
            if h is not None:
                on_reprint(h)

        def _on_void_event(e):
            try:
                row_id = int(e.args)
            except (TypeError, ValueError):
                return
            h = by_id.get(row_id)
            if h is not None and on_void is not None:
                on_void(h)

        table.on("reprint", _on_reprint_event)
        table.on("void", _on_void_event)


# ─── Dialog d'annulation SSCC (utilisé par _do_void) ────────────────────────

def _fmt_sscc_short(sscc: str) -> str:
    """Affichage compact d'un SSCC pour les notifications."""
    s = re.sub(r"\D+", "", sscc or "")
    if len(s) != 18:
        return s
    return f"{s[0:4]} {s[4:8]} {s[8:12]} {s[12:16]} {s[16:18]}"


def _open_void_dialog(*, sscc: str, designation: str, on_confirmed) -> None:
    """Petit dialog : demande une raison + confirme l'annulation.

    Le ``on_confirmed`` est appelé avec la raison (str non-vide).
    """
    import inspect

    with ui.dialog() as dlg, ui.card().classes("q-pa-md").style("min-width: 380px"):
        ui.label("Annuler cette étiquette ?").classes("text-h6").style(
            f"color: {COLORS['ink']}; font-weight: 700",
        )
        ui.label(designation).classes("text-body2 q-mb-xs").style(
            f"color: {COLORS['ink']}",
        )
        ui.label(f"SSCC : {_fmt_sscc_short(sscc)}").classes("text-caption q-mb-md").style(
            f"font-family: monospace; color: {COLORS['ink2']}",
        )
        ui.label(
            "La palette ne sera plus proposée au chargement et le SSCC "
            "ne pourra plus servir. Le séquentiel reste consommé "
            "(non-réutilisable, norme GS1).",
        ).classes("text-caption q-mb-md").style(f"color: {COLORS['ink2']}")
        reason_input = ui.input(
            label="Raison",
            placeholder="ex: pas imprimée — doublon",
        ).classes("w-full").props("outlined dense autofocus")

        async def _submit():
            reason = (reason_input.value or "").strip()
            if not reason:
                ui.notify("Saisis une raison.", type="warning")
                return
            dlg.close()
            res = on_confirmed(reason)
            if inspect.isawaitable(res):
                await res

        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Annuler", on_click=dlg.close).props("flat color=grey-7")
            ui.button(
                "Confirmer l'annulation", icon="block", on_click=_submit,
            ).props("color=red-7 unelevated")
        reason_input.on("keydown.enter", _submit)
    dlg.open()


def _install_scan_input_listener() -> None:
    """Injecte le JS qui écoute le file input + upload + emitEvent vers Python.

    Idempotent : ``window._fsScanInputBound`` empêche le double-binding si la
    page est ré-ouverte sans full reload (édge case Quasar/NiceGUI).
    """
    ui.add_body_html(_SCAN_INPUT_LISTENER_JS)


def _ctx_from_entry(
    entry: LabelEntry, count: int,
    *, full_pallet: bool, n_copies: int, tenant_name: str, sscc: str = "",
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
        sscc=sscc,
    )


def _ctx_from_history(h: HistoryEntry, *, tenant_name: str):
    """Construit un EtiquetteContext depuis une HistoryEntry (réimpression).

    Le SSCC stocké est réutilisé tel quel : une réimpression vise la
    MÊME palette physique (cas où l'opérateur a perdu l'étiquette
    originale), pas une nouvelle.
    """
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
        sscc=h.sscc,
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
