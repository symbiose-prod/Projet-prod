"""
pages/chargement_camion.py
==========================
Page « Chargement camion » — scan SSCC palette → génération automatique
d'un Bon de Livraison + envoi email logisticien + téléchargement PDF.

Workflow opérateur :
  1. Choisit la date de ramasse + destinataire (Sofripa, etc.)
  2. (optionnel) Sélectionne une ramasse existante du jour à mettre à
     jour (mode v2+, sinon création nouvelle ramasse).
  3. Scanne les SSCC des palettes chargées sur le camion (caméra iOS
     ou saisie manuelle SSCC).
  4. Le panier se construit en live ; les doublons sont ignorés, les
     palettes déjà chargées sont bloquées.
  5. Une fois le camion plein, valide → save/update ramasse + email +
     PDF BL téléchargé pour le chauffeur.

Cette page vit en PARALLÈLE de /ramasse (qui reste manuelle).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import re

from nicegui import app, ui

from common.audit import ACTION_RAMASSE_SAVED
from common.email import send_html_with_pdf
from common.ramasse import load_destinataires, today_paris
from common.ramasse_history import (
    get_ramasse,
    list_ramasses,
    save_ramasse,
    update_ramasse,
)
from common.services.loading_service import (
    PaletteInfo,
    aggregate_palettes_to_lines,
    link_palettes_to_ramasse,
    list_unscanned_recent_palettes,
    lookup_sscc,
)
from common.services.ramasse_service import (
    build_email_body,
    build_email_subject,
)
from common.xlsx_fill.bl_pdf import build_bl_enlevements_pdf
from pages.auth import require_auth
from pages.theme import COLORS, page_layout

_log = logging.getLogger("ferment.chargement_camion")


def _step_title(num: int, title: str, icon: str = "") -> None:
    """Pastille numérotée + titre — cohérent avec /etiquettes-palette."""
    with ui.element("div").classes("section-header row items-center gap-2"):
        with ui.element("div").style(
            f"width: 28px; height: 28px; border-radius: 50%; "
            f"background: {COLORS['green']}; color: white; "
            "display: inline-flex; align-items: center; justify-content: center; "
            "font-weight: 700; font-size: 14px; flex-shrink: 0",
        ):
            ui.label(str(num))
        if icon:
            ui.icon(icon, size="xs").style(f"color: {COLORS['green']}")
        ui.label(title).classes("text-subtitle1").style(
            f"color: {COLORS['ink']}; font-weight: 600",
        )


def _fmt_sscc_pretty(sscc: str) -> str:
    """3377 0014 4200 0000 05 — pour lisibilité humaine."""
    s = re.sub(r"\D+", "", sscc or "")
    if len(s) != 18:
        return s
    return f"{s[0:4]} {s[4:8]} {s[8:12]} {s[12:16]} {s[16:18]}"


@ui.page("/chargement-camion")
async def page_chargement_camion():
    user = require_auth()
    if not user:
        return

    tenant_id = user.get("tenant_id", "")
    user_email = user.get("email", "")

    with page_layout("Chargement camion", "local_shipping", "/chargement-camion"):
        ui.label(
            "Scanne les SSCC des palettes au moment du chargement. "
            "À la validation : email logisticien + BL téléchargeable."
        ).classes("text-body2").style(f"color: {COLORS['ink2']}")

        _render_form(tenant_id=tenant_id, user_email=user_email)


# ─── UI principale ──────────────────────────────────────────────────────────

_BASKET_STORAGE_KEY = "chargement_camion_basket"


def _serialize_palette(p: PaletteInfo) -> dict:
    """Sérialise une PaletteInfo pour app.storage.user (JSON-safe)."""
    return {
        "sscc": p.sscc, "gtin_palette": p.gtin_palette, "lot": p.lot,
        "ddm": p.ddm.isoformat() if p.ddm else None,
        "case_count": p.case_count, "designation": p.designation,
        "fmt": p.fmt, "marque": p.marque, "gout": p.gout,
        "pcb": p.pcb, "gtin_uvc": p.gtin_uvc,
        "generated_at": p.generated_at.isoformat() if p.generated_at else None,
    }


def _deserialize_palette(d: dict) -> PaletteInfo | None:
    """Reconstruit une PaletteInfo depuis le dict stocké. None si invalide."""
    try:
        ddm_str = d.get("ddm")
        ddm = _dt.date.fromisoformat(ddm_str) if ddm_str else None
        gen_str = d.get("generated_at")
        gen = _dt.datetime.fromisoformat(gen_str) if gen_str else _dt.datetime.now()
        return PaletteInfo(
            sscc=str(d.get("sscc") or ""),
            gtin_palette=str(d.get("gtin_palette") or ""),
            lot=str(d.get("lot") or ""),
            ddm=ddm,
            case_count=int(d.get("case_count") or 0),
            designation=str(d.get("designation") or ""),
            fmt=str(d.get("fmt") or ""),
            marque=str(d.get("marque") or ""),
            gout=str(d.get("gout") or ""),
            pcb=int(d.get("pcb") or 0),
            gtin_uvc=str(d.get("gtin_uvc") or ""),
            generated_at=gen,
        )
    except (TypeError, ValueError, KeyError):
        return None


def _render_form(*, tenant_id: str, user_email: str) -> None:
    """Rend le wizard 3 étapes."""

    # Restore le panier depuis app.storage.user si on revient après un reload.
    # Permet à l'opérateur de scanner 20 palettes, perdre la page (iOS swap,
    # crash réseau, reload accidentel) et retrouver son travail intact.
    persisted_basket = app.storage.user.get(_BASKET_STORAGE_KEY) or []
    restored_basket: list[PaletteInfo] = []
    if isinstance(persisted_basket, list):
        for d in persisted_basket:
            p = _deserialize_palette(d) if isinstance(d, dict) else None
            if p is not None:
                restored_basket.append(p)

    state: dict = {
        "date_ramasse": today_paris(),
        "destinataire": None,
        "ramasse_to_update_id": None,  # None = créer nouvelle ; sinon UUID
        "basket": restored_basket,     # list[PaletteInfo] — restauré si reload
    }

    if restored_basket:
        ui.notify(
            f"✓ Session restaurée — {len(restored_basket)} palette(s) "
            "déjà dans le panier.",
            type="info", icon="restore", timeout=4500,
        )

    def _persist_basket():
        """Sauvegarde le panier dans app.storage.user après chaque modif."""
        try:
            app.storage.user[_BASKET_STORAGE_KEY] = [
                _serialize_palette(p) for p in state["basket"]
            ]
        except Exception:
            _log.warning("Persistance panier échouée", exc_info=True)

    def _clear_persisted_basket():
        """Vide le panier persisté (après validation ou bouton clear)."""
        try:
            app.storage.user[_BASKET_STORAGE_KEY] = []
        except Exception:
            pass

    destinataires_list = load_destinataires()

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 1 — Destination
    # ────────────────────────────────────────────────────────────────────
    _step_title(1, "Destination", "place")
    with ui.card().classes("w-full q-pa-md").props("flat bordered"):
        with ui.row().classes("w-full gap-3 items-end no-wrap"):
            date_input = ui.input(
                label="Date de ramasse",
                value=today_paris().strftime("%Y-%m-%d"),
            ).classes("flex-1").props("outlined dense type=date")

            # Les destinataires utilisent la clé "name" (pas "title").
            # Format réel : {"name", "address_lines", "email_recipients",
            #                "packaging_items"} — voir data/destinataires.json.
            dest_opts = [d["name"] for d in destinataires_list]
            default_dest = dest_opts[0] if dest_opts else None
            dest_select = ui.select(
                options=dest_opts, label="Destinataire",
                value=default_dest,
            ).classes("flex-1").props("outlined dense")

        # Ramasse existante à mettre à jour ? Liste des ramasses non
        # verrouillées (driver_passed=False) du tenant, triées par date.
        ramasse_select_row = ui.row().classes("w-full q-mt-sm")
        with ramasse_select_row:
            ramasse_to_update_select = ui.select(
                options={"": "— Créer une nouvelle ramasse —"},
                label="Mettre à jour une ramasse existante ?",
                value="",
            ).classes("w-full").props("outlined dense")

        def _refresh_existing_ramasses():
            """Recharge la liste des ramasses ouvertes (driver_passed=False)."""
            try:
                ramasses = list_ramasses(tenant_id=tenant_id, limit=15)
            except Exception:
                _log.warning("Échec list_ramasses", exc_info=True)
                ramasses = []
            opts = {"": "— Créer une nouvelle ramasse —"}
            for r in ramasses:
                if r.get("driver_passed"):
                    continue
                if r.get("deleted_at"):
                    continue
                dr = r.get("date_ramasse")
                dest = r.get("destinataire", "?")
                version = r.get("version", 1)
                label = f"{dr} · {dest} (v{version}, {r.get('total_palettes', 0)} pal)"
                opts[str(r["id"])] = label
            ramasse_to_update_select.options = opts
            ramasse_to_update_select.update()

        _refresh_existing_ramasses()

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 2 — Scan des palettes
    # ────────────────────────────────────────────────────────────────────
    _step_title(2, "Scanner les palettes", "qr_code_scanner")
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
            'Scanner un SSCC'
            '<input type="file" id="sscc-capture-input" '
            'accept="image/*" capture="environment" '
            'style="position:absolute; inset:0; opacity:0; cursor:pointer; '
            'width:100%; height:100%;">'
            '</label>',
        )

    with ui.row().classes("w-full justify-center q-mb-md"):
        ui.button(
            "Saisir le SSCC à la main",
            icon="keyboard",
            on_click=lambda: _open_manual_sscc_dialog(_handle_manual_sscc),
        ).props("outline color=grey-8")

    _install_sscc_scan_listener()

    # Panier de palettes scannées
    basket_card = ui.card().classes("w-full q-pa-none q-mt-sm").props("flat bordered")
    with basket_card:
        with ui.card_section().classes("q-pa-sm"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                basket_header = ui.label("Panier vide").classes("text-subtitle2").style(
                    f"color: {COLORS['ink2']}",
                )
                # Bouton "vider le panier" — utile si la session restaurée
                # n'est plus à jour (palettes déjà validées ailleurs, etc.)
                clear_basket_btn = ui.button(
                    "Vider", icon="delete_sweep",
                ).props("flat dense color=grey-7 size=sm")
                clear_basket_btn.set_visibility(False)
        basket_list_container = ui.column().classes("w-full gap-0")

    # Palettes non scannées (rappel visuel)
    with ui.expansion(
        text="Palettes étiquetées récemment mais pas encore chargées",
        icon="visibility",
    ).classes("w-full q-mt-sm").props("dense"):
        unscanned_container = ui.column().classes("w-full gap-1 q-pa-sm")

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 3 — Récap & validation
    # ────────────────────────────────────────────────────────────────────
    _step_title(3, "Récap & validation", "summarize")
    summary_card = ui.card().classes("w-full q-pa-md").props("flat bordered")
    with summary_card:
        # Totaux gros
        with ui.column().classes("w-full items-center gap-0"):
            ui.label("CHARGÉ SUR LE CAMION").classes("text-caption").style(
                f"color: {COLORS['ink2']}; letter-spacing: 1px; font-weight: 600",
            )
            totals_display = ui.label("0 palettes · 0 cartons").style(
                f"color: {COLORS['ink2']}; font-weight: 700; "
                "font-size: 28px; text-align: center; line-height: 1.2",
            )
            weight_display = ui.label("").classes("text-body2").style(
                f"color: {COLORS['ink2']}",
            )

        ui.separator().classes("q-my-md")

        # Détail par produit (table simple)
        ui.label("Détail par produit").classes("text-caption").style(
            f"color: {COLORS['ink2']}; letter-spacing: 1px; font-weight: 600",
        )
        details_container = ui.column().classes("w-full q-mt-xs")

    # Bouton validation principal — flow standard (création/MAJ + email + PDF)
    with ui.row().classes("w-full q-mt-md gap-2"):
        validate_btn = ui.button(
            "✓ Valider — créer ramasse + envoyer email + télécharger BL",
            icon="check_circle",
        ).classes("flex-1").props(
            "color=green-8 unelevated size=lg",
        ).style("touch-action: manipulation; min-height: 56px")
        validate_btn.disable()

    # Bouton rattrapage — secondaire, visible UNIQUEMENT quand une ramasse
    # existante est sélectionnée pour MAJ. Sert au cas où le BL a déjà été
    # envoyé via /ramasse (la page manuelle) mais qu'on veut lier les
    # palettes scannées pour qu'elles n'apparaissent plus en "non chargées".
    # Aucun email, aucun PDF, aucune nouvelle version : juste l'INSERT
    # palette_loadings.
    with ui.row().classes("w-full q-mt-xs gap-2"):
        link_only_btn = ui.button(
            "🔗 Rattrapage : lier seulement les palettes (sans email/BL)",
            icon="link",
        ).classes("flex-1").props(
            "color=grey-7 outline size=md",
        ).style("touch-action: manipulation; min-height: 44px; font-size: 13px")
        link_only_btn.set_visibility(False)
        link_only_btn.disable()

    # ────────────────────────────────────────────────────────────────────
    # Logique réactive
    # ────────────────────────────────────────────────────────────────────

    def _refresh_basket():
        """Reconstruit la liste du panier + le récap totaux."""
        basket: list[PaletteInfo] = state["basket"]
        basket_list_container.clear()
        if not basket:
            basket_header.text = "Panier vide — scanne un SSCC pour commencer"
            basket_header.style(f"color: {COLORS['ink2']}")
            clear_basket_btn.set_visibility(False)
        else:
            basket_header.text = f"Panier : {len(basket)} palette(s)"
            basket_header.style(f"color: {COLORS['ink']}; font-weight: 600")
            clear_basket_btn.set_visibility(True)
            with basket_list_container:
                for i, p in enumerate(basket):
                    if i > 0:
                        ui.separator()
                    with ui.row().classes(
                        "w-full items-center q-pa-sm gap-3 no-wrap",
                    ):
                        with ui.column().classes("flex-1 gap-0"):
                            ui.label(_fmt_sscc_pretty(p.sscc)).classes(
                                "text-caption",
                            ).style(
                                "font-family: monospace; color: " + COLORS["ink2"],
                            )
                            ui.label(
                                f"{p.designation} {p.fmt}",
                            ).classes("text-body2").style(
                                f"color: {COLORS['ink']}; font-weight: 500",
                            )
                            ddm_str = p.ddm.strftime("%d/%m/%Y") if p.ddm else "—"
                            ui.label(
                                f"Lot {p.lot} · {p.case_count} cartons · DDM {ddm_str}",
                            ).classes("text-caption").style(
                                f"color: {COLORS['ink2']}",
                            )
                        ui.button(
                            icon="close",
                            on_click=lambda _e, sscc=p.sscc: _remove_from_basket(sscc),
                        ).props("flat dense color=grey-7 round").style(
                            "touch-action: manipulation",
                        )
        _refresh_summary()

    def _refresh_link_only_visibility():
        """Affiche le bouton rattrapage uniquement si on a une ramasse
        existante sélectionnée ET au moins une palette dans le panier."""
        ramasse_val = ramasse_to_update_select.value
        has_ramasse = bool(ramasse_val) and ramasse_val != ""
        has_basket = bool(state["basket"])
        link_only_btn.set_visibility(has_ramasse and has_basket)
        if has_ramasse and has_basket:
            link_only_btn.enable()
        else:
            link_only_btn.disable()

    def _refresh_summary():
        """Recalcule totaux + détail produit. Active/désactive le bouton."""
        basket: list[PaletteInfo] = state["basket"]
        details_container.clear()
        if not basket:
            totals_display.text = "0 palettes · 0 cartons"
            totals_display.style(f"color: {COLORS['ink2']}; font-size: 28px")
            weight_display.text = ""
            validate_btn.disable()
            _refresh_link_only_visibility()
            return
        lines = aggregate_palettes_to_lines(basket)
        total_cartons = sum(line["cartons"] for line in lines)
        total_palettes = sum(line["palettes"] for line in lines)
        total_poids = sum(line["poids"] for line in lines)
        totals_display.text = f"{total_palettes} palettes · {total_cartons} cartons"
        totals_display.style(
            f"color: {COLORS['green']}; font-weight: 700; "
            "font-size: 32px; text-align: center; line-height: 1.2",
        )
        weight_display.text = f"≈ {total_poids:,} kg".replace(",", " ")
        with details_container:
            for line in lines:
                with ui.row().classes(
                    "w-full items-center q-pa-xs",
                ).style(f"border-top: 1px solid {COLORS['border']}"):
                    ui.label(line["produit"]).classes("text-body2").style(
                        "flex: 2",
                    )
                    ui.label(f"{line['palettes']} pal").classes(
                        "text-body2",
                    ).style(f"flex: 0.6; text-align: right; color: {COLORS['ink2']}")
                    ui.label(f"{line['cartons']} c").classes(
                        "text-body2",
                    ).style(f"flex: 0.6; text-align: right; color: {COLORS['ink2']}")
                    ui.label(f"{line['poids']:,} kg".replace(",", " ")).classes(
                        "text-body2",
                    ).style(f"flex: 0.8; text-align: right; color: {COLORS['ink2']}")
        validate_btn.enable()
        _refresh_link_only_visibility()

    def _add_to_basket(palette: PaletteInfo):
        # Dédoublonnage : si le SSCC est déjà dans le panier, notify et ignore
        basket: list[PaletteInfo] = state["basket"]
        if any(p.sscc == palette.sscc for p in basket):
            ui.notify(
                "Palette déjà scannée — ignorée.",
                type="warning", icon="info", timeout=2500,
            )
            return
        basket.append(palette)
        ui.notify(
            f"✓ {palette.designation} {palette.fmt} — {palette.case_count} cartons",
            type="positive", icon="check", timeout=2000,
        )
        _persist_basket()
        _refresh_basket()

    def _remove_from_basket(sscc: str):
        state["basket"] = [p for p in state["basket"] if p.sscc != sscc]
        ui.notify("Palette retirée du panier.", type="info", timeout=1500)
        _persist_basket()
        _refresh_basket()

    def _handle_scan_result(data):
        """Reçoit le résultat d'un scan SSCC (caméra ou EAN manuel) et
        décide quoi en faire."""
        if not isinstance(data, dict):
            return
        status = str(data.get("status") or "")
        if status == "ok" and data.get("palette"):
            p = data["palette"]
            ddm_str = p.get("ddm")
            ddm = _dt.date.fromisoformat(ddm_str) if ddm_str else None
            gen_at = p.get("generated_at")
            try:
                gen_dt = _dt.datetime.fromisoformat(gen_at) if gen_at else _dt.datetime.now()
            except (ValueError, TypeError):
                gen_dt = _dt.datetime.now()
            palette = PaletteInfo(
                sscc=str(p.get("sscc") or ""),
                gtin_palette=str(p.get("gtin_palette") or ""),
                lot=str(p.get("lot") or ""),
                ddm=ddm,
                case_count=int(p.get("case_count") or 0),
                designation=str(p.get("designation") or ""),
                fmt=str(p.get("fmt") or ""),
                marque=str(p.get("marque") or ""),
                gout=str(p.get("gout") or ""),
                pcb=int(p.get("pcb") or 0),
                gtin_uvc=str(p.get("gtin_uvc") or ""),
                generated_at=gen_dt,
            )
            _add_to_basket(palette)
        elif status == "already_loaded":
            ui.notify(
                "⚠ Palette déjà chargée sur une autre ramasse — refusée.",
                type="negative", icon="block", timeout=5000,
            )
        elif status == "unknown":
            err = str(data.get("error") or "SSCC inconnu")
            # Phase 3 : on offrira ici un dialog "Créer cette palette".
            # Pour l'instant, on notify.
            ui.notify(
                f"⚠ {err}",
                type="warning", icon="warning", timeout=5000,
            )
        elif status == "inconsistent":
            ui.notify(
                "⚠ Anomalie DB : SSCC connu mais infos produit manquantes. "
                "Investigation requise.",
                type="negative", icon="error", timeout=6000,
            )
        else:
            ui.notify(f"Statut inattendu : {status}", type="warning")

    async def _handle_manual_sscc(sscc: str):
        """Saisie manuelle : POST /api/lookup-sscc, puis _handle_scan_result."""
        cleaned = (sscc or "").strip()
        if not cleaned:
            return
        ui.notify("🔍 Recherche…", type="info", timeout=1500)
        try:
            result = await asyncio.to_thread(lookup_sscc, cleaned, tenant_id)
        except Exception:
            _log.exception("Erreur lookup_sscc (manuel)")
            ui.notify("Erreur de recherche.", type="negative")
            return
        # Sérialisation comme le ferait l'endpoint
        data = {
            "status": result.status,
            "palette": None,
            "error": result.error_message,
        }
        if result.palette:
            p = result.palette
            data["palette"] = {
                "sscc": p.sscc, "gtin_palette": p.gtin_palette,
                "lot": p.lot, "ddm": p.ddm.isoformat() if p.ddm else None,
                "case_count": p.case_count, "designation": p.designation,
                "fmt": p.fmt, "marque": p.marque, "gout": p.gout,
                "pcb": p.pcb, "gtin_uvc": p.gtin_uvc,
                "generated_at": p.generated_at.isoformat() if p.generated_at else None,
            }
        _handle_scan_result(data)

    # Wiring des events scan (caméra → /api/scan-sscc → emitEvent)
    ui.on("sscc_scanned", lambda e: _handle_scan_result(e.args))
    ui.on(
        "sscc_error",
        lambda e: ui.notify(f"Scan : {e.args}", type="warning", timeout=4000),
    )
    ui.on(
        "sscc_uploading",
        lambda e: ui.notify("📤 Analyse…", type="info", timeout=1500),
    )

    # ── Palettes non scannées (rappel) ──
    def _refresh_unscanned():
        unscanned_container.clear()
        try:
            recent = list_unscanned_recent_palettes(tenant_id, days=7, limit=30)
        except Exception:
            _log.warning("Échec list_unscanned_recent_palettes", exc_info=True)
            recent = []
        with unscanned_container:
            if not recent:
                ui.label("Toutes les palettes récentes ont été chargées 🎉").classes(
                    "text-caption q-pa-sm",
                ).style(f"color: {COLORS['ink2']}; font-style: italic")
                return
            # Bandeau résumé + bouton bulk "Tout ajouter au panier"
            with ui.row().classes("w-full items-center justify-between no-wrap q-pa-xs"):
                ui.label(
                    f"{len(recent)} palette(s) étiquetée(s) ces 7 derniers jours :",
                ).classes("text-caption").style(f"color: {COLORS['ink2']}")
                ui.button(
                    "Tout ajouter au panier", icon="playlist_add_check",
                    on_click=lambda: _add_all_unscanned_to_basket(recent),
                ).props("outline dense color=blue-7 size=sm")
            for p in recent:
                with ui.row().classes("w-full items-center q-py-xs no-wrap"):
                    ui.label(_fmt_sscc_pretty(p.sscc)).classes("text-caption").style(
                        "font-family: monospace; flex: 1; "
                        f"color: {COLORS['ink2']}",
                    )
                    ui.label(
                        f"{p.designation} {p.fmt} · {p.case_count}c",
                    ).classes("text-caption").style(
                        f"flex: 2; color: {COLORS['ink']}",
                    )
                    ui.label(
                        p.generated_at.strftime("%d/%m %H:%M")
                        if hasattr(p.generated_at, "strftime") else "",
                    ).classes("text-caption").style(
                        f"flex: 0.8; color: {COLORS['ink2']}",
                    )
                    # Bouton "Ajouter au panier" → ajoute juste cette palette
                    ui.button(
                        icon="add_shopping_cart",
                        on_click=lambda _e, palette=p: _add_unscanned_to_basket(palette),
                    ).props("flat dense color=blue-7 size=sm").tooltip(
                        "Ajouter au panier (sans scanner)",
                    )
                    # Bouton "Erreur d'impression" → ouvre dialog d'annulation
                    ui.button(
                        icon="block",
                        on_click=lambda _e, palette=p: _open_void_palette_dialog(palette),
                    ).props("flat dense color=red-7 size=sm").tooltip(
                        "Erreur d'impression — annuler cette palette fantôme",
                    )

    def _add_unscanned_to_basket(palette: PaletteInfo):
        """Ajoute une palette (déjà connue côté DB) au panier sans scanner.

        Utile pour le rattrapage : les palettes sont parties physiquement
        mais on a besoin de les marquer comme chargées rétroactivement.
        """
        basket: list[PaletteInfo] = state["basket"]
        if any(p.sscc == palette.sscc for p in basket):
            ui.notify("Déjà dans le panier.", type="info", timeout=1500)
            return
        basket.append(palette)
        _persist_basket()
        _refresh_basket()
        ui.notify(
            f"+ {palette.designation} {palette.fmt}",
            type="positive", timeout=1500,
        )

    def _add_all_unscanned_to_basket(palettes: list[PaletteInfo]):
        """Ajoute toutes les palettes non chargées au panier (rattrapage en masse)."""
        basket: list[PaletteInfo] = state["basket"]
        existing_ssccs = {p.sscc for p in basket}
        added = 0
        for palette in palettes:
            if palette.sscc in existing_ssccs:
                continue
            basket.append(palette)
            existing_ssccs.add(palette.sscc)
            added += 1
        if added > 0:
            _persist_basket()
            _refresh_basket()
            ui.notify(
                f"✓ {added} palette(s) ajoutée(s) au panier. "
                "Sélectionne la ramasse puis clique 'Rattrapage'.",
                type="positive", icon="playlist_add_check", timeout=5000,
            )
        else:
            ui.notify("Toutes ces palettes sont déjà dans le panier.", type="info")

    def _open_void_palette_dialog(palette: PaletteInfo):
        """Ouvre un dialog pour annuler une palette qui apparait dans les
        non-chargées alors qu'elle n'existe pas physiquement (étiquette
        pas imprimée, doublon…)."""
        with ui.dialog() as dlg, ui.card().classes("q-pa-md").style("min-width: 380px"):
            ui.label("Erreur d'impression ?").classes("text-h6").style(
                f"color: {COLORS['ink']}; font-weight: 700",
            )
            ui.label(
                f"{palette.designation} {palette.fmt} · {palette.case_count} cartons",
            ).classes("text-body2 q-mb-xs").style(f"color: {COLORS['ink']}")
            ui.label(f"SSCC : {_fmt_sscc_pretty(palette.sscc)}").classes(
                "text-caption q-mb-md",
            ).style(f"font-family: monospace; color: {COLORS['ink2']}")
            ui.label(
                "La palette ne sera plus proposée au chargement. "
                "Le SSCC reste consommé (norme GS1).",
            ).classes("text-caption q-mb-md").style(f"color: {COLORS['ink2']}")
            reason_input = ui.input(
                label="Raison",
                value="Étiquette pas imprimée",
                placeholder="ex: étiquette pas imprimée, doublon…",
            ).classes("w-full").props("outlined dense autofocus")

            async def _confirm():
                reason = (reason_input.value or "").strip() or "Erreur d'impression"
                dlg.close()
                from common.services.sscc_service import void_sscc
                ok = await asyncio.to_thread(
                    void_sscc, tenant_id, palette.sscc,
                    reason=reason, user_email=user_email,
                )
                if ok:
                    ui.notify(
                        f"✓ Palette {_fmt_sscc_pretty(palette.sscc)} annulée.",
                        type="positive", icon="block", timeout=3500,
                    )
                    _refresh_unscanned()
                else:
                    ui.notify(
                        "Annulation impossible (déjà annulée ?).",
                        type="warning",
                    )

            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Annuler", on_click=dlg.close).props("flat color=grey-7")
                ui.button(
                    "Confirmer l'annulation", icon="block", on_click=_confirm,
                ).props("color=red-7 unelevated")
        dlg.open()

    _refresh_unscanned()

    # ── Validation finale ──
    async def _on_validate():
        basket: list[PaletteInfo] = state["basket"]
        if not basket:
            ui.notify("Panier vide.", type="warning")
            return

        # Récupère le state UI
        d_str = (date_input.value or "").strip()
        try:
            d = _dt.date.fromisoformat(d_str) if d_str else today_paris()
        except ValueError:
            ui.notify(f"Date invalide : {d_str}", type="negative")
            return

        dest_title = (dest_select.value or "").strip()
        if not dest_title:
            ui.notify("Sélectionne un destinataire.", type="warning")
            return

        # Trouve l'objet destinataire complet pour le PDF (adresse + emails)
        dest_obj = next(
            (x for x in destinataires_list if x.get("name") == dest_title), None,
        )
        if not dest_obj:
            ui.notify(f"Destinataire inconnu : {dest_title}", type="negative")
            return
        dest_addr_lines = dest_obj.get("address_lines", []) or []
        dest_emails = dest_obj.get("email_recipients", []) or []
        if not dest_emails:
            ui.notify(
                "Le destinataire n'a pas d'email configuré — impossible d'envoyer.",
                type="negative",
            )
            return

        validate_btn.disable()
        validate_btn.props("loading")

        try:
            # ── 1. Agrège les lignes au format ramasse ──
            lines = aggregate_palettes_to_lines(basket)
            total_cartons = sum(line["cartons"] for line in lines)
            total_palettes = sum(line["palettes"] for line in lines)
            total_poids = sum(line["poids"] for line in lines)

            # ── 2. Détermine mode (create vs update) ──
            ramasse_id_to_update = (
                state["ramasse_to_update_id"]
                or (ramasse_to_update_select.value if ramasse_to_update_select.value else None)
            )
            is_update = bool(ramasse_id_to_update)
            next_version = 1
            previous_lines = None
            if is_update:
                existing = await asyncio.to_thread(
                    get_ramasse, ramasse_id_to_update, tenant_id=tenant_id,
                )
                if not existing:
                    ui.notify(
                        "Ramasse à mettre à jour introuvable. Annule et recommence.",
                        type="negative",
                    )
                    return
                if existing.get("driver_passed"):
                    ui.notify(
                        "Ramasse verrouillée (chauffeur déjà passé). "
                        "Crée une nouvelle ramasse.",
                        type="negative",
                    )
                    return
                next_version = int(existing.get("version") or 1) + 1
                previous_lines = existing.get("lines") or []
                # Fusionne avec les lignes existantes : un update v2+ ajoute
                # AUX lignes existantes (pas remplacement) — cohérent avec le
                # cas métier "ramasse créée J1 soir, complétée J2 matin".
                # On agrège par produit pour fusionner les colonnes (ref/produit).
                merged = _merge_lines(previous_lines, lines)
                lines = merged
                total_cartons = sum(line["cartons"] for line in lines)
                total_palettes = sum(line["palettes"] for line in lines)
                total_poids = sum(line["poids"] for line in lines)

            # ── 3. Build le PDF BL ──
            df_lines = _build_df_for_pdf(lines)
            pdf_bytes = await asyncio.to_thread(
                build_bl_enlevements_pdf,
                date_creation=today_paris(),
                date_ramasse=d,
                destinataire_title=dest_title,
                destinataire_lines=dest_addr_lines,
                df_lines=df_lines,
                previous_lines=previous_lines,
                version=next_version,
            )

            # ── 4. Sauvegarde ramasse (create ou update) ──
            sender_email = os.environ.get("EMAIL_SENDER") or ""
            recipients = list(dest_emails)
            if sender_email and sender_email not in recipients:
                recipients.append(sender_email)

            if is_update:
                await asyncio.to_thread(
                    update_ramasse,
                    ramasse_id_to_update,
                    date_ramasse=d,
                    destinataire=dest_title,
                    recipients=recipients,
                    lines=lines,
                    total_cartons=total_cartons,
                    total_palettes=total_palettes,
                    total_poids_kg=total_poids,
                    pdf_bytes=pdf_bytes,
                    tenant_id=tenant_id,
                )
                ramasse_id_final = ramasse_id_to_update
            else:
                ramasse_id_final = await asyncio.to_thread(
                    save_ramasse,
                    date_ramasse=d,
                    destinataire=dest_title,
                    recipients=recipients,
                    lines=lines,
                    total_cartons=total_cartons,
                    total_palettes=total_palettes,
                    total_poids_kg=total_poids,
                    pdf_bytes=pdf_bytes,
                    tenant_id=tenant_id,
                )

            # ── 5. Lie les palettes à la ramasse ──
            sscc_list = [p.sscc for p in basket]
            inserted, conflicts = await asyncio.to_thread(
                link_palettes_to_ramasse,
                tenant_id,
                sscc_list=sscc_list,
                ramasse_id=str(ramasse_id_final),
                user_email=user_email,
            )
            if conflicts:
                _log.warning(
                    "Loading : %d palettes en conflict UNIQUE — déjà liées ailleurs : %s",
                    len(conflicts), conflicts[:5],
                )
                ui.notify(
                    f"⚠ {len(conflicts)} palette(s) déjà liée(s) à une autre ramasse — "
                    "vérifier le journal admin.",
                    type="warning", timeout=6000,
                )

            # ── 6. Envoie l'email ──
            subject = build_email_subject(
                d, is_update=is_update, version=next_version,
            )
            body = build_email_body(
                d, total_palettes=total_palettes, total_cartons=total_cartons,
                packaging_lines=None,
                is_update=is_update, version=next_version,
            )
            fname = f"BL_Chargement_{d:%Y%m%d}.pdf"
            await asyncio.to_thread(
                send_html_with_pdf,
                to_email=recipients, subject=subject,
                html_body=body, attachments=[(fname, pdf_bytes)],
            )

            # ── 7. Télécharge le PDF pour le chauffeur ──
            ui.download(pdf_bytes, fname)
            ui.notify(
                f"✓ Ramasse {'mise à jour (v' + str(next_version) + ')' if is_update else 'créée'} — "
                f"{inserted} palette(s) liée(s), email envoyé.",
                type="positive", icon="check_circle", timeout=6000,
            )

            # Reset (panier + persistance)
            state["basket"] = []
            _clear_persisted_basket()
            _refresh_basket()
            _refresh_unscanned()
            _refresh_existing_ramasses()
        except Exception as exc:
            _log.exception("Erreur validation chargement camion")
            ui.notify(f"Erreur : {exc}", type="negative", timeout=8000)
        finally:
            validate_btn.enable()
            validate_btn.props(remove="loading")

    validate_btn.on_click(_on_validate)

    async def _on_link_only():
        """Mode rattrapage : lie le panier à la ramasse sélectionnée sans
        envoyer d'email ni regénérer le PDF.

        Use case : le BL a déjà été envoyé via /ramasse (page manuelle),
        on veut juste enregistrer la traçabilité physique des palettes
        scannées pour qu'elles disparaissent de la liste "non chargées".
        """
        basket: list[PaletteInfo] = state["basket"]
        if not basket:
            ui.notify("Panier vide.", type="warning")
            return
        ramasse_id = ramasse_to_update_select.value
        if not ramasse_id or ramasse_id == "":
            ui.notify(
                "Sélectionne une ramasse existante (le rattrapage la met "
                "à jour sans toucher au BL).",
                type="warning",
            )
            return

        link_only_btn.disable()
        link_only_btn.props("loading")
        try:
            sscc_list = [p.sscc for p in basket]
            inserted, conflicts = await asyncio.to_thread(
                link_palettes_to_ramasse,
                tenant_id,
                sscc_list=sscc_list,
                ramasse_id=str(ramasse_id),
                user_email=user_email,
            )
            if conflicts:
                _log.warning(
                    "Rattrapage : %d conflits (palettes déjà liées) : %s",
                    len(conflicts), conflicts[:5],
                )
                ui.notify(
                    f"⚠ {len(conflicts)} palette(s) déjà liée(s) ailleurs — ignorée(s).",
                    type="warning", timeout=5000,
                )
            ui.notify(
                f"✓ Rattrapage : {inserted} palette(s) liée(s) à la ramasse. "
                "Aucun email envoyé.",
                type="positive", icon="link", timeout=5000,
            )
            # Reset panier
            state["basket"] = []
            _clear_persisted_basket()
            _refresh_basket()
            _refresh_unscanned()
        except Exception as exc:
            _log.exception("Erreur rattrapage link")
            ui.notify(f"Erreur : {exc}", type="negative")
        finally:
            link_only_btn.enable()
            link_only_btn.props(remove="loading")

    link_only_btn.on_click(_on_link_only)

    # Quand le select ramasse change → re-évaluer la visibilité du bouton rattrapage
    ramasse_to_update_select.on_value_change(lambda _e: _refresh_link_only_visibility())

    def _on_clear_basket():
        """Bouton manuel : vide le panier persisté + l'état courant."""
        state["basket"] = []
        _clear_persisted_basket()
        ui.notify("Panier vidé.", type="info", timeout=1500)
        _refresh_basket()

    clear_basket_btn.on_click(_on_clear_basket)

    # Initial render — affichera les palettes restaurées si présentes
    _refresh_basket()


# ─── Helpers ────────────────────────────────────────────────────────────────

def _merge_lines(previous: list[dict], new: list[dict]) -> list[dict]:
    """Fusionne 2 listes de lignes ramasse par (produit, ref).

    Utilisé en mode v2+ pour ajouter les palettes scannées du matin J2
    AUX lignes saisies / scannées en J1 soir.
    """
    by_key: dict[tuple[str, str], dict] = {}
    for line in previous:
        key = (str(line.get("produit") or ""), str(line.get("ref") or ""))
        by_key[key] = dict(line)
    for line in new:
        key = (str(line.get("produit") or ""), str(line.get("ref") or ""))
        if key in by_key:
            existing = by_key[key]
            existing["cartons"] = int(existing.get("cartons") or 0) + int(line.get("cartons") or 0)
            existing["palettes"] = int(existing.get("palettes") or 0) + int(line.get("palettes") or 0)
            existing["poids"] = int(existing.get("poids") or 0) + int(line.get("poids") or 0)
            # On garde la DDM la plus proche
            if line.get("ddm") and (
                not existing.get("ddm") or line["ddm"] < existing["ddm"]
            ):
                existing["ddm"] = line["ddm"]
        else:
            by_key[key] = dict(line)
    return sorted(by_key.values(), key=lambda r: r.get("produit", ""))


def _build_df_for_pdf(lines: list[dict]):
    """Construit le DataFrame attendu par build_bl_enlevements_pdf."""
    import pandas as pd
    return pd.DataFrame([
        {
            "Référence": line.get("ref", ""),
            "Produit": line.get("produit", ""),
            "DDM": line.get("ddm", ""),
            "Cartons": int(line.get("cartons") or 0),
            "Palettes": int(line.get("palettes") or 0),
            "Poids (kg)": int(line.get("poids") or 0),
        }
        for line in lines
    ])


# ─── Saisie manuelle SSCC (fallback caméra) ─────────────────────────────────

def _open_manual_sscc_dialog(handler) -> None:
    """Petit dialog pour saisir un SSCC à la main (18 digits)."""
    import inspect

    with ui.dialog() as dlg, ui.card().classes("q-pa-md").style("min-width: 340px"):
        ui.label("Saisie manuelle SSCC").classes("text-subtitle1")
        ui.label("18 chiffres — avec ou sans le préfixe (00)").classes(
            "text-caption q-mb-sm",
        ).style(f"color: {COLORS['ink2']}")
        sscc_input = ui.input(
            placeholder="(00) 3 37700 14420 00000 05",
        ).classes("w-full").props("outlined dense autofocus")

        async def _submit():
            val = (sscc_input.value or "").strip()
            if not val:
                return
            dlg.close()
            res = handler(val)
            if inspect.isawaitable(res):
                await res

        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Annuler", on_click=dlg.close).props("flat color=grey-7")
            ui.button(
                "Valider", icon="check", on_click=_submit,
            ).props("color=green-8 unelevated")
        sscc_input.on("keydown.enter", _submit)
    dlg.open()


# ─── JS scan listener (caméra iOS → /api/scan-sscc → emitEvent) ─────────────

_SSCC_SCAN_LISTENER_JS = """
<script>
(function() {
    if (window._fsScanSsccBound) return;
    window._fsScanSsccBound = true;

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
                    canvas.getContext('2d').drawImage(img, 0, 0, w, h);
                    canvas.toBlob(
                        (blob) => blob ? resolve(blob) : reject(new Error('toBlob')),
                        'image/jpeg', 0.85,
                    );
                };
                img.src = reader.result;
            };
            reader.readAsDataURL(file);
        });
    }

    function _fsFeedback(success) {
        try {
            if (navigator.vibrate) navigator.vibrate(success ? [60, 30, 60] : [200]);
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
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.18);
            osc.connect(gain).connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.2);
            setTimeout(() => { try { ctx.close(); } catch (e) {} }, 300);
        } catch (e) { /* noop */ }
    }

    const wait = () => {
        const input = document.getElementById('sscc-capture-input');
        if (!input) { setTimeout(wait, 200); return; }
        input.addEventListener('change', async (e) => {
            const file = e.target.files && e.target.files[0];
            if (!file) return;
            emitEvent('sscc_uploading', file.size);
            try {
                let toUpload;
                try {
                    toUpload = await _fsResizeImage(file, 1280);
                } catch (err) {
                    toUpload = file;
                }
                const formData = new FormData();
                formData.append('file', toUpload, 'photo.jpg');
                const resp = await fetch('/api/scan-sscc', {
                    method: 'POST', body: formData,
                });
                const data = await resp.json();
                if (data.status === 'ok' || data.status === 'already_loaded' ||
                    data.status === 'unknown' || data.status === 'inconsistent') {
                    _fsFeedback(data.status === 'ok');
                    emitEvent('sscc_scanned', data);
                } else {
                    _fsFeedback(false);
                    emitEvent('sscc_error', (data.error || 'Erreur'));
                }
            } catch (err) {
                _fsFeedback(false);
                emitEvent('sscc_error', String(err));
            }
            e.target.value = '';
        });
    };
    wait();
})();
</script>
"""


def _install_sscc_scan_listener() -> None:
    """Injecte le JS qui écoute le file input + upload + emit vers Python."""
    ui.add_body_html(_SSCC_SCAN_LISTENER_JS)


# Suppression import inutilisé — laisse un comment pour traçabilité.
_ = ACTION_RAMASSE_SAVED  # noqa: F841 — réservé pour audit log futur
