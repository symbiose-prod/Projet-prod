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

Seul flow de ramasse depuis la refonte 2026-05 (l'ancienne saisie
manuelle /ramasse a été retirée — toutes les palettes sont étiquetées
dès leur fabrication et scannées au chargement).
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
from common.ramasse import (
    build_packaging_summary,
    fmt_paris,
    load_destinataires,
    load_packaging_items,
    today_paris,
)
from common.ramasse_history import (
    get_active_ramasse_for_dest,
    get_last_packaging_for_dest,
    get_ramasse,
    list_ramasses,
    save_ramasse,
    update_ramasse,
)
from common.services.loading_service import (
    PaletteInfo,
    aggregate_palettes_to_lines,
    link_palettes_to_ramasse,
    list_linked_palettes,
    list_palettes_in_cold_room,
    list_unscanned_recent_palettes,
    lookup_sscc,
    rebuild_lines_from_palettes,
    unlink_palette,
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

    with page_layout("Ramasse / Chargement camion", "local_shipping", "/chargement-camion"):
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
    # BANDEAU D'ÉTAT — visible en permanence en haut de la page
    # ────────────────────────────────────────────────────────────────────
    # Raconte qui a fait quoi sur la ramasse en cours. Crucial dans le
    # contexte multi-utilisateurs (Max crée le prévisionnel J1, Mohamed
    # le finalise J2). Mis à jour à chaque action et au changement de
    # destinataire.
    state_banner_container = ui.column().classes("w-full q-mb-md")

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

        # Cache du statut des ramasses listées — sert à piloter la
        # visibilité / le label des boutons d'envoi.
        ramasse_status_by_id: dict[str, str] = {}

        def _refresh_existing_ramasses():
            """Recharge la liste des ramasses non-livrées éligibles à update.

            Exclut les ramasses ``legacy`` (créées avant la refonte) et
            celles déjà livrées ou supprimées. Le statut courant
            (``previsionnel`` / ``definitif``) est annoté dans le label
            pour que l'opérateur sache où en est chaque ramasse.
            """
            try:
                ramasses = list_ramasses(tenant_id=tenant_id, limit=15)
            except Exception:
                _log.warning("Échec list_ramasses", exc_info=True)
                ramasses = []
            opts = {"": "— Créer une nouvelle ramasse —"}
            ramasse_status_by_id.clear()
            for r in ramasses:
                if r.get("driver_passed"):
                    continue
                if r.get("deleted_at"):
                    continue
                status = str(r.get("status") or "")
                if status == "legacy":
                    # Anciennes ramasses /ramasse : ne participent pas au
                    # workflow prévisionnel/définitif scan-driven.
                    continue
                dr = r.get("date_ramasse")
                dest = r.get("destinataire", "?")
                status_label = {
                    "previsionnel": "PRÉV",
                    "definitif": "DÉF",
                }.get(status, status[:5].upper() if status else "?")
                rid = str(r["id"])
                ramasse_status_by_id[rid] = status
                opts[rid] = (
                    f"{dr} · {dest} [{status_label}] · "
                    f"{r.get('total_palettes', 0)} pal"
                )
            ramasse_to_update_select.options = opts
            ramasse_to_update_select.update()

        _refresh_existing_ramasses()

        # Liste des palettes déjà liées à la ramasse sélectionnée — pour
        # permettre de retirer du BL une palette pas prête / cassée /
        # erronée sans annuler le SSCC. La section est visible uniquement
        # en mode update (ramasse non vide sélectionnée). Le refresh est
        # piloté par _refresh_linked_palettes (au changement du select).
        linked_palettes_container = ui.column().classes("w-full q-mt-sm")

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 2 — Palettes en chambre froide (toujours visible)
    # ────────────────────────────────────────────────────────────────────
    # Snapshot du stock : palettes étiquetées non chargées sur la
    # ramasse en cours. Sert de base à la demande provisoire (toutes
    # incluses automatiquement) ET au chargement (l'opérateur voit ce
    # qui reste à scanner).
    _step_title(2, "Palettes en chambre froide", "ac_unit")
    cold_room_container = ui.column().classes("w-full q-mt-sm")

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 3 — Scanner les palettes chargées dans le camion
    # ────────────────────────────────────────────────────────────────────
    # Visible UNIQUEMENT quand une ramasse provisoire est en cours
    # (i.e. status='previsionnel' ou 'definitif' non livré). Le scan
    # bascule la palette de "en chambre froide" vers "dans le camion"
    # via palette_loadings. La saisie est physique : chaque palette
    # scannée = chargée pour de bon.
    scan_section = ui.column().classes("w-full")
    with scan_section:
        _step_title(3, "Scanner les palettes chargées", "qr_code_scanner")
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
        basket_card = ui.card().classes("w-full q-pa-none q-mt-sm").props(
            "flat bordered",
        )
        with basket_card:
            with ui.card_section().classes("q-pa-sm"):
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    basket_header = ui.label("Panier vide").classes(
                        "text-subtitle2",
                    ).style(f"color: {COLORS['ink2']}")
                    clear_basket_btn = ui.button(
                        "Vider", icon="delete_sweep",
                    ).props("flat dense color=grey-7 size=sm")
                    clear_basket_btn.set_visibility(False)
            basket_list_container = ui.column().classes("w-full gap-0")

    # Section "Palettes étiquetées récemment mais pas encore chargées"
    # (legacy / rattrapage) — gardée mais cachée par défaut, car
    # remplacée par la section CF principale en mode opérationnel.
    unscanned_container = ui.column().classes("w-full gap-1 q-pa-sm")
    unscanned_container.set_visibility(False)

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 4 — Emballages à demander au logisticien
    # ────────────────────────────────────────────────────────────────────
    # Bouteilles vides etc. stockées chez le logisticien : on les demande
    # généralement au prévisionnel pour qu'il les prépare et les amène
    # avec le camion lors du chargement. Conservé aussi sur le définitif.
    _step_title(4, "Emballages à ramener", "move_to_inbox")
    packaging_state: dict = {"items": []}
    packaging_container = ui.column().classes("w-full")

    def _get_packaging_lines() -> list[dict] | None:
        """Retourne les emballages saisis (qty > 0) ou None."""
        summary = build_packaging_summary(packaging_state["items"])
        return summary if summary else None

    def _build_packaging_section():
        """Construit la section emballages pour le destinataire courant.

        Réutilisée à chaque changement de destinataire — l'état des qty
        déjà saisies est préservé tant que les labels matchent. La
        première saisie peut être pré-remplie avec les « quantités
        habituelles » (dernière ramasse pour ce dest).
        """
        saved_pkg_qty: dict[str, int] = {
            str(it.get("label") or ""): int(it.get("qty") or 0)
            for it in packaging_state["items"]
            if int(it.get("qty") or 0) > 0
        }
        packaging_state["items"] = []
        dest_name = dest_select.value or ""
        pkg_items = load_packaging_items(dest_name)
        if not pkg_items:
            ui.label("Pas d'emballages configurés pour ce destinataire.").classes(
                "text-caption q-pa-sm",
            ).style(f"color: {COLORS['ink2']}; font-style: italic")
            return

        usual_pkg_qty: dict[str, int] = {}
        if not saved_pkg_qty:
            try:
                last_pkg = get_last_packaging_for_dest(dest_name)
                usual_pkg_qty = {
                    str(p.get("label") or ""): int(p.get("qty") or 0)
                    for p in last_pkg
                    if p.get("label") and int(p.get("qty") or 0) > 0
                }
            except Exception:
                _log.warning("Échec chargement emballages habituels", exc_info=True)

        qty_inputs_by_label: dict = {}

        if usual_pkg_qty:
            usual_summary = ", ".join(
                f"{q} {label}" for label, q in usual_pkg_qty.items()
            )
            with ui.row().classes(
                "w-full items-center gap-2 q-pa-sm q-mb-sm",
            ).style(
                "background: #EFF6FF; border: 1px dashed #93C5FD; border-radius: 6px",
            ):
                ui.icon("history", color="blue-7", size="sm")
                with ui.column().classes("flex-1 gap-0"):
                    ui.label("Quantités habituelles (dernière ramasse)").classes(
                        "text-caption",
                    ).style("color: #1E3A8A; font-weight: 600")
                    ui.label(usual_summary).classes("text-caption").style(
                        "color: #1E40AF",
                    )

                def _apply_usual():
                    for label, qty in usual_pkg_qty.items():
                        inp = qty_inputs_by_label.get(label)
                        if inp is not None:
                            inp.value = qty
                            for it in packaging_state["items"]:
                                if it["label"] == label:
                                    it["qty"] = qty
                                    break
                    ui.notify("Quantités habituelles appliquées.",
                              type="info", icon="check")

                ui.button("Appliquer", icon="check",
                          on_click=_apply_usual).props("flat dense color=blue-7")

        for item in pkg_items:
            initial_qty = saved_pkg_qty.get(item["label"], 0)
            item_state = {
                "id": item["id"],
                "label": item["label"],
                "unit": item.get("unit", "palette"),
                "qty": initial_qty,
            }
            packaging_state["items"].append(item_state)
            with ui.row().classes("w-full items-center gap-3 q-py-xs"):
                ui.label(item["label"]).classes("flex-1 text-body2")
                qty_input = ui.number(
                    value=initial_qty, min=0, step=1,
                ).props("outlined dense").style("max-width: 100px")
                qty_inputs_by_label[item["label"]] = qty_input
                ui.label(item.get("unit", "palette")).classes(
                    "text-caption text-grey-6",
                )

                def _on_qty(_e, st=item_state, inp=qty_input):
                    st["qty"] = int(inp.value or 0)

                qty_input.on("update:model-value", _on_qty)

    with packaging_container:
        _build_packaging_section()

    def _refresh_packaging(_e=None):
        packaging_container.clear()
        with packaging_container:
            _build_packaging_section()

    dest_select.on_value_change(_refresh_packaging)

    # ────────────────────────────────────────────────────────────────────
    # ÉTAPE 5 — Récap & validation
    # ────────────────────────────────────────────────────────────────────
    _step_title(5, "Récap & validation", "summarize")
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

    # ── Boutons d'envoi : prévisionnel + définitif ──
    # Le prévisionnel sert au logisticien à dimensionner son camion
    # (estimation indicative). Le définitif est le BL rectificatif envoyé
    # au moment du chargement, qui reflète exactement ce qui part. La
    # transition definitif → previsionnel est interdite (régression).
    with ui.row().classes("w-full q-mt-md gap-2"):
        prev_btn = ui.button(
            "Envoyer prévisionnel",
            icon="schedule_send",
        ).classes("flex-1").props(
            "color=blue-7 outline size=lg",
        ).style("touch-action: manipulation; min-height: 56px")
        prev_btn.disable()

        def_btn = ui.button(
            "Envoyer BL définitif",
            icon="check_circle",
        ).classes("flex-1").props(
            "color=green-8 unelevated size=lg",
        ).style("touch-action: manipulation; min-height: 56px")
        def_btn.disable()

    # Bouton rattrapage — secondaire, visible UNIQUEMENT quand une ramasse
    # existante est sélectionnée pour MAJ. Sert au cas où le BL a déjà été
    # envoyé manuellement (ou pour les ramasses legacy d'avant la refonte)
    # mais qu'on veut lier les palettes scannées pour qu'elles
    # n'apparaissent plus en "non chargées". Aucun email, aucun PDF,
    # aucune nouvelle version : juste l'INSERT palette_loadings.
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

    def _current_target_status() -> str | None:
        """Statut de la ramasse pré-sélectionnée (None si create)."""
        rid = ramasse_to_update_select.value
        if not rid or rid == "":
            return None
        return ramasse_status_by_id.get(str(rid))

    # Cache des palettes liées à la ramasse pré-sélectionnée — populé par
    # _refresh_linked_palettes et lu par _refresh_summary pour distinguer
    # « déjà liées » vs « panier scanné maintenant ». Pas une source de
    # vérité (le BL est rebuild depuis la DB au moment du send) — juste
    # un cache d'affichage qu'on rafraîchit aux mêmes moments que la
    # liste UI.
    linked_cache: dict[str, list[PaletteInfo]] = {"items": []}

    def _refresh_linked_palettes():
        """Recharge la liste des palettes déjà liées à la ramasse sélectionnée.

        Vide si pas de ramasse sélectionnée (mode create). Sinon affiche
        une expansion repliable avec une ligne par palette + bouton
        « Retirer du BL » → dialog raison → unlink + refresh.
        """
        linked_palettes_container.clear()
        rid = ramasse_to_update_select.value
        if not rid or rid == "":
            linked_cache["items"] = []
            _refresh_summary()  # nettoie le récap « déjà liées »
            return
        try:
            linked = list_linked_palettes(str(rid), tenant_id)
        except Exception:
            _log.warning("Échec list_linked_palettes", exc_info=True)
            linked = []
        linked_cache["items"] = linked

        with linked_palettes_container:
            if not linked:
                ui.label("Aucune palette liée à cette ramasse pour l'instant.").classes(
                    "text-caption q-pa-sm",
                ).style(f"color: {COLORS['ink2']}; font-style: italic")
                return

            with ui.expansion(
                text=f"Palettes déjà liées au BL ({len(linked)})",
                icon="link",
                value=False,
            ).classes("w-full").props("dense header-class='text-subtitle2'"):
                ui.label(
                    "Tu peux retirer une palette du BL si elle n'est finalement "
                    "pas chargée (palette pas prête, cassée, erreur de scan…). "
                    "Le SSCC reste valide et la palette redevient disponible "
                    "pour une autre ramasse.",
                ).classes("text-caption q-pa-sm").style(
                    f"color: {COLORS['ink2']}",
                )
                for p in linked:
                    with ui.row().classes(
                        "w-full items-center q-pa-sm gap-3 no-wrap",
                    ).style(
                        f"border-top: 1px solid {COLORS['border']}",
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
                            "Retirer du BL", icon="link_off",
                            on_click=(
                                lambda _e, palette=p, rid=str(rid):
                                _open_unlink_dialog(palette, rid)
                            ),
                        ).props("flat dense color=orange-8").style(
                            "touch-action: manipulation",
                        )

    def _open_unlink_dialog(palette: PaletteInfo, ramasse_id: str):
        """Dialog de confirmation + raison pour délier une palette du BL."""
        with ui.dialog() as dlg, ui.card().classes("q-pa-md").style("min-width: 380px"):
            ui.label("Retirer cette palette du BL ?").classes("text-h6").style(
                f"color: {COLORS['ink']}; font-weight: 700",
            )
            ui.label(
                f"{palette.designation} {palette.fmt} · "
                f"{palette.case_count} cartons",
            ).classes("text-body2 q-mb-xs").style(f"color: {COLORS['ink']}")
            ui.label(f"SSCC : {_fmt_sscc_pretty(palette.sscc)}").classes(
                "text-caption q-mb-md",
            ).style(f"font-family: monospace; color: {COLORS['ink2']}")
            ui.label(
                "La palette ne fera plus partie de cette ramasse. Son SSCC "
                "reste valide — elle réapparaîtra dans la liste « non "
                "chargées » et pourra être liée à une autre ramasse.",
            ).classes("text-caption q-mb-md").style(f"color: {COLORS['ink2']}")
            reason_input = ui.input(
                label="Raison",
                placeholder="ex: palette pas prête, cassée, erreur scan…",
            ).classes("w-full").props("outlined dense autofocus")

            async def _confirm():
                reason = (reason_input.value or "").strip()
                if not reason:
                    ui.notify("Saisis une raison.", type="warning")
                    return
                dlg.close()
                try:
                    ok = await asyncio.to_thread(
                        unlink_palette,
                        tenant_id,
                        sscc=palette.sscc,
                        ramasse_id=ramasse_id,
                        reason=reason,
                        user_email=user_email,
                    )
                except Exception as exc:
                    _log.exception("Erreur unlink_palette")
                    ui.notify(f"Erreur : {exc}", type="negative")
                    return
                if not ok:
                    ui.notify(
                        "Palette déjà retirée ou liée à une autre ramasse.",
                        type="warning",
                    )
                    return
                ui.notify(
                    f"✓ Palette {palette.designation} {palette.fmt} retirée du BL.",
                    type="positive", icon="link_off", timeout=4000,
                )
                # Refresh : la palette disparaît de la liste « liées »,
                # réapparaît en CF (un unlink rebascule la palette
                # « dans le camion » → « en chambre froide »). Le bandeau
                # d'état se met à jour aussi (total_palettes a changé).
                _refresh_linked_palettes()
                _refresh_unscanned()
                _refresh_existing_ramasses()
                _refresh_state_banner()
                _refresh_cold_room()

            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Annuler", on_click=dlg.close).props("flat color=grey-7")
                ui.button(
                    "Retirer du BL", icon="link_off", on_click=_confirm,
                ).props("color=orange-8 unelevated")
            reason_input.on("keydown.enter", _confirm)
        dlg.open()

    def _refresh_action_buttons():
        """Adapte les boutons et la visibilité du scan selon le mode.

        3 modes, déterminés par le statut de la ramasse pré-sélectionnée :

        - **A. Aucune ramasse active** (mode « Demande provisoire ») :
          - scan_section CACHÉE — on ne scanne rien pour un prévisionnel.
          - prev_btn « Envoyer demande provisoire » (gros vert primaire).
          - def_btn caché (le définitif n'a de sens qu'après un provisoire).
          - link_only_btn caché.

        - **B. Provisoire en cours** (mode « Chargement ») :
          - scan_section VISIBLE — chaque scan = palette dans le camion.
          - def_btn « Envoyer BL définitif » (gros vert primaire).
          - prev_btn « Renvoyer la demande provisoire » (outline, rare).
          - link_only_btn si panier non vide.

        - **C. Définitif envoyé** (en attente chauffeur) :
          - scan_section VISIBLE pour corrections.
          - def_btn « Corriger BL définitif » (orange).
          - prev_btn caché (régression interdite).
        """
        has_basket = bool(state["basket"])
        has_linked = bool(linked_cache.get("items"))
        current_status = _current_target_status()
        rid = ramasse_to_update_select.value
        has_ramasse = bool(rid) and rid != ""

        if current_status == "definitif":
            # Mode C : correction d'un définitif
            scan_section.set_visibility(True)
            prev_btn.set_visibility(False)
            def_btn.set_visibility(True)
            def_btn.text = "Corriger BL définitif"
            can_send = has_basket or has_linked
            link_only_btn.set_visibility(has_ramasse and has_basket)
        elif current_status == "previsionnel":
            # Mode B : provisoire en cours, on charge le camion
            scan_section.set_visibility(True)
            prev_btn.set_visibility(True)
            prev_btn.text = "Renvoyer demande provisoire"
            def_btn.set_visibility(True)
            def_btn.text = "Envoyer BL définitif"
            # Pour un définitif on a besoin de palettes scannées ou liées
            # Pour un renvoi prévisionnel : toujours OK (snapshot CF auto)
            can_send = True  # le prévisionnel ne dépend pas du panier
        else:
            # Mode A : pas de ramasse → demande provisoire
            scan_section.set_visibility(False)
            prev_btn.set_visibility(True)
            prev_btn.text = "Envoyer demande provisoire"
            def_btn.set_visibility(False)
            # Le prévisionnel inclut auto toutes les palettes en CF —
            # le bouton est toujours actif (la liste CF peut être vide,
            # auquel cas l'envoi sera refusé avec un message clair).
            can_send = True
            link_only_btn.set_visibility(False)

        if can_send:
            prev_btn.enable()
            def_btn.enable()
        else:
            prev_btn.disable()
            def_btn.disable()

        # Rattrapage : reste piloté par dest + panier (mode B/C uniquement)
        if current_status in (None, ""):
            link_only_btn.set_visibility(False)
        if link_only_btn.visible:
            if has_ramasse and has_basket:
                link_only_btn.enable()
            else:
                link_only_btn.disable()

    def _refresh_summary():
        """Recalcule totaux + détail produit. Distingue ce qui est déjà
        au BL (palettes liées en DB) de ce qu'on s'apprête à ajouter
        (panier scanné maintenant).

        En mode create (pas de ramasse à updater) : récap classique du
        panier uniquement.
        En mode update : 3 lignes — « déjà au BL », « + à ajouter »,
        « Total après envoi ». Le détail par produit est calculé sur la
        somme (linked ∪ basket) pour refléter le BL final.
        """
        basket: list[PaletteInfo] = state["basket"]
        linked: list[PaletteInfo] = linked_cache.get("items") or []
        details_container.clear()
        is_update_mode = bool(linked) or bool(
            ramasse_to_update_select.value
            and ramasse_to_update_select.value != "",
        )

        # ── Pas de panier ET pas de ramasse → vraiment vide ──
        if not basket and not linked:
            totals_display.text = "0 palettes · 0 cartons"
            totals_display.style(f"color: {COLORS['ink2']}; font-size: 28px")
            weight_display.text = ""
            _refresh_action_buttons()
            return

        # Totaux par bucket
        basket_lines = aggregate_palettes_to_lines(basket) if basket else []
        linked_lines = aggregate_palettes_to_lines(linked) if linked else []
        # Combinés pour le détail par produit (BL final après envoi)
        combined_lines = aggregate_palettes_to_lines(list(linked) + list(basket))

        b_palettes = sum(line["palettes"] for line in basket_lines)
        b_cartons = sum(line["cartons"] for line in basket_lines)
        l_palettes = sum(line["palettes"] for line in linked_lines)
        l_cartons = sum(line["cartons"] for line in linked_lines)
        c_palettes = sum(line["palettes"] for line in combined_lines)
        c_cartons = sum(line["cartons"] for line in combined_lines)
        c_poids = sum(line["poids"] for line in combined_lines)

        # ── Mode update : récap composite ──
        if is_update_mode:
            totals_display.text = (
                f"{c_palettes} palettes · {c_cartons} cartons"
            )
            totals_display.style(
                f"color: {COLORS['green']}; font-weight: 700; "
                "font-size: 32px; text-align: center; line-height: 1.2",
            )
            breakdown_lines = []
            if l_palettes:
                breakdown_lines.append(
                    f"{l_palettes} déjà liée(s) au BL ({l_cartons} cartons)",
                )
            if b_palettes:
                breakdown_lines.append(
                    f"+ {b_palettes} scannée(s) à ajouter ({b_cartons} cartons)",
                )
            weight_display.text = (
                "  ·  ".join(breakdown_lines) + f"  ≈  {c_poids:,} kg"
            ).replace(",", " ")
        else:
            # ── Mode create : panier seul ──
            totals_display.text = (
                f"{b_palettes} palettes · {b_cartons} cartons"
            )
            totals_display.style(
                f"color: {COLORS['green']}; font-weight: 700; "
                "font-size: 32px; text-align: center; line-height: 1.2",
            )
            b_poids = sum(line["poids"] for line in basket_lines)
            weight_display.text = f"≈ {b_poids:,} kg".replace(",", " ")

        # Détail par produit (combiné — c'est le BL final après envoi).
        # En mode update, on indique « (BL après envoi) » pour clarifier.
        with details_container:
            if is_update_mode and basket:
                ui.label("Détail combiné (BL après envoi)").classes(
                    "text-caption q-pa-xs",
                ).style(f"color: {COLORS['ink2']}; font-style: italic")
            for line in combined_lines:
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
        _refresh_action_buttons()

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
        _refresh_cold_room()

    def _remove_from_basket(sscc: str):
        state["basket"] = [p for p in state["basket"] if p.sscc != sscc]
        ui.notify("Palette retirée du panier.", type="info", timeout=1500)
        _persist_basket()
        _refresh_basket()
        _refresh_cold_room()

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
    def _refresh_cold_room():
        """Rebuild la section « Palettes en chambre froide ».

        Affiche le stock physique actuel (palettes étiquetées non
        chargées sur une ramasse active). Sert :
        - en mode A (aucune ramasse) : à montrer ce qui sera inclus
          automatiquement dans la prochaine demande provisoire,
        - en mode B (provisoire en cours) : à montrer ce qui reste à
          scanner au chargement. Les palettes déjà dans le panier sont
          marquées « ✓ Scannée pour le camion ».

        Grouped par produit + format pour lisibilité (un compteur par
        groupe), avec un récap total en haut. La PR de polish visuel
        (mini-cartes par produit, photo, couleur DDM) viendra dessus.
        """
        cold_room_container.clear()
        try:
            palettes = list_palettes_in_cold_room(tenant_id)
        except Exception:
            _log.warning("Échec list_palettes_in_cold_room", exc_info=True)
            palettes = []

        basket_ssccs = {p.sscc for p in state["basket"]}

        with cold_room_container:
            if not palettes:
                with ui.row().classes("w-full items-center gap-2 q-pa-md").style(
                    f"background: #F9FAFB; border: 1px dashed {COLORS['border']}; "
                    "border-radius: 8px",
                ):
                    ui.icon("inbox", size="md").style(f"color: {COLORS['ink2']}")
                    ui.label(
                        "Chambre froide vide — aucune palette étiquetée "
                        "disponible. Étiquette des palettes depuis "
                        "/etiquettes-palette pour les ajouter au stock.",
                    ).classes("text-body2").style(
                        f"color: {COLORS['ink2']}; font-style: italic",
                    )
                return

            total_palettes = len(palettes)
            total_cartons = sum(int(p.case_count or 0) for p in palettes)

            # Bandeau récap global
            with ui.row().classes(
                "w-full items-center justify-between q-pa-sm",
            ).style(
                "background: #ECFDF5; border: 1px solid #6EE7B7; "
                "border-radius: 8px",
            ):
                with ui.column().classes("gap-0"):
                    ui.label("STOCK CHAMBRE FROIDE").classes("text-caption").style(
                        "color: #065F46; letter-spacing: 1px; font-weight: 700",
                    )
                    ui.label(
                        f"{total_palettes} palette(s) · {total_cartons} cartons",
                    ).classes("text-h6").style(
                        "color: #047857; font-weight: 700",
                    )
                if basket_ssccs:
                    nb_in_basket = sum(
                        1 for p in palettes if p.sscc in basket_ssccs
                    )
                    ui.label(
                        f"dont {nb_in_basket} dans le panier",
                    ).classes("text-caption").style(
                        "color: #047857; font-weight: 600",
                    )

            # Détail groupé par produit + format
            groups: dict[tuple[str, str], list[PaletteInfo]] = {}
            for p in palettes:
                key = (p.designation, p.fmt)
                groups.setdefault(key, []).append(p)

            for (designation, fmt), group_palettes in sorted(
                groups.items(), key=lambda kv: kv[0][0],
            ):
                nb = len(group_palettes)
                cartons = sum(int(p.case_count or 0) for p in group_palettes)
                # Compter combien de cette ligne sont scannées dans le panier
                nb_scanned = sum(
                    1 for p in group_palettes if p.sscc in basket_ssccs
                )
                with ui.row().classes(
                    "w-full items-center gap-3 q-pa-sm q-mt-xs",
                ).style(
                    f"border-top: 1px solid {COLORS['border']}",
                ):
                    with ui.column().classes("flex-1 gap-0"):
                        ui.label(f"{designation} {fmt}").classes(
                            "text-body2",
                        ).style(f"color: {COLORS['ink']}; font-weight: 500")
                        ddms = sorted(
                            [p.ddm for p in group_palettes if p.ddm],
                        )
                        if ddms:
                            first_ddm = ddms[0]
                            ddm_str = first_ddm.strftime("%d/%m/%Y") if hasattr(first_ddm, "strftime") else str(first_ddm)
                            ui.label(
                                f"DDM la plus proche : {ddm_str}",
                            ).classes("text-caption").style(
                                f"color: {COLORS['ink2']}",
                            )
                    ui.label(f"{nb} pal").classes("text-body2").style(
                        f"color: {COLORS['ink']}; font-weight: 600; "
                        "min-width: 60px; text-align: right",
                    )
                    ui.label(f"{cartons} c").classes("text-body2").style(
                        f"color: {COLORS['ink2']}; "
                        "min-width: 60px; text-align: right",
                    )
                    if nb_scanned > 0:
                        ui.label(
                            f"✓ {nb_scanned} scannée(s)",
                        ).classes("text-caption").style(
                            "color: #047857; font-weight: 600; "
                            "min-width: 100px; text-align: right",
                        )

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
                        fmt_paris(p.generated_at, "%d/%m %H:%M"),
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
    async def _on_validate(target_status: str):
        """Orchestre l'envoi d'un prévisionnel ou d'un définitif.

        Les 2 flux ont des sémantiques très différentes :

        - ``previsionnel`` : snapshot automatique de la chambre froide
          (toutes les palettes étiquetées non chargées). Pas de scan,
          pas de lien ``palette_loadings`` — c'est une prévision pour
          dimensionner le camion. Le snapshot est stocké dans
          ``ramasse_history.lines``.

        - ``definitif`` : palettes physiquement chargées dans le camion,
          identifiées par le scan SSCC pendant le chargement. Le BL
          reflète exactement ``palette_loadings``. Doit faire suite à
          un prévisionnel (mode update obligatoire).

        Verrou « 1 ramasse active par dest » (cf. has_active_ramasse) :
        si Max essaie de créer un 2e prévisionnel alors qu'un BL n'est
        pas encore livré, on refuse et on rafraîchit l'UI.
        """
        # ── State commun aux 2 modes ──
        basket: list[PaletteInfo] = state["basket"]
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

        ramasse_id_to_update = (
            state["ramasse_to_update_id"]
            or (ramasse_to_update_select.value if ramasse_to_update_select.value else None)
        )
        is_update = bool(ramasse_id_to_update)
        previous_lines_for_diff: list[dict] | None = None
        next_version = 1

        # Validation préalable du mode update
        if is_update:
            existing = await asyncio.to_thread(
                get_ramasse, ramasse_id_to_update, tenant_id=tenant_id,
            )
            if not existing:
                ui.notify(
                    "Ramasse à mettre à jour introuvable. Recharge la page.",
                    type="negative",
                )
                return
            if existing.get("driver_passed"):
                ui.notify(
                    "Ramasse verrouillée (chauffeur déjà passé). "
                    "Marque-la comme non livrée pour la modifier.",
                    type="negative",
                )
                return
            next_version = int(existing.get("version") or 1) + 1
            previous_lines_for_diff = existing.get("lines") or []

        # ── Validation spécifique au mode ──
        if target_status == "definitif":
            if not is_update:
                ui.notify(
                    "Envoie d'abord la demande provisoire (palettes en chambre froide), "
                    "puis fais le BL définitif au moment du chargement.",
                    type="negative", timeout=10000,
                )
                return
            # Définitif : panier doit être non vide OU des palettes
            # doivent déjà être liées (cas correction sans nouveau scan).
            has_linked = bool(linked_cache.get("items"))
            if not basket and not has_linked:
                ui.notify(
                    "Scanne au moins une palette du camion avant d'envoyer "
                    "le BL définitif.",
                    type="warning",
                )
                return

        sender_email = os.environ.get("EMAIL_SENDER") or ""
        recipients = list(dest_emails)
        if sender_email and sender_email not in recipients:
            recipients.append(sender_email)

        prev_btn.disable()
        prev_btn.props("loading")
        def_btn.disable()
        def_btn.props("loading")

        ramasse_id_final: str | None = None

        try:
            if target_status == "previsionnel":
                # ─── Mode PROVISOIRE — snapshot automatique de la CF ────
                palettes_cf = await asyncio.to_thread(
                    list_palettes_in_cold_room, tenant_id,
                )
                if not palettes_cf:
                    ui.notify(
                        "Aucune palette en chambre froide à inclure dans la demande.",
                        type="warning", timeout=6000,
                    )
                    return
                lines = aggregate_palettes_to_lines(palettes_cf)
                total_cartons = sum(int(line["cartons"]) for line in lines)
                total_palettes = sum(int(line["palettes"]) for line in lines)
                total_poids = sum(int(line["poids"]) for line in lines)

                packaging_lines_ui = _get_packaging_lines()

                df_lines = _build_df_for_pdf(lines)
                pdf_bytes = await asyncio.to_thread(
                    build_bl_enlevements_pdf,
                    date_creation=today_paris(),
                    date_ramasse=d,
                    destinataire_title=dest_title,
                    destinataire_lines=dest_addr_lines,
                    df_lines=df_lines,
                    packaging_lines=packaging_lines_ui,
                    previous_lines=None,  # pas de diff sur un prévisionnel
                    version=next_version,
                    kind="previsionnel",
                )

                if is_update:
                    # Renvoyer un prévisionnel mis à jour (option 1)
                    result = await asyncio.to_thread(
                        update_ramasse,
                        ramasse_id_to_update,
                        date_ramasse=d,
                        destinataire=dest_title,
                        recipients=recipients,
                        lines=lines,
                        total_cartons=total_cartons,
                        total_palettes=total_palettes,
                        total_poids_kg=total_poids,
                        packaging=packaging_lines_ui,
                        pdf_bytes=pdf_bytes,
                        target_status="previsionnel",
                        tenant_id=tenant_id,
                    )
                    if result is None:
                        ui.notify(
                            "Transition refusée (un BL définitif ne peut pas "
                            "redevenir prévisionnel).",
                            type="negative", timeout=8000,
                        )
                        return
                    ramasse_id_final = str(ramasse_id_to_update)
                else:
                    # Création directe — pas de placeholder + finalize,
                    # on a déjà toutes les lignes (depuis la CF).
                    try:
                        ramasse_id_final = await asyncio.to_thread(
                            save_ramasse,
                            date_ramasse=d,
                            destinataire=dest_title,
                            recipients=recipients,
                            lines=lines,
                            total_cartons=total_cartons,
                            total_palettes=total_palettes,
                            total_poids_kg=total_poids,
                            packaging=packaging_lines_ui,
                            pdf_bytes=pdf_bytes,
                            status="previsionnel",
                            tenant_id=tenant_id,
                        )
                    except ValueError as exc:
                        # Verrou « 1 ramasse active par dest »
                        ui.notify(
                            str(exc), type="negative", icon="lock",
                            timeout=10000, position="top",
                        )
                        _refresh_existing_ramasses()
                        _refresh_state_banner()
                        return

                inserted = total_palettes  # pour le message final

            else:  # target_status == "definitif"
                # ─── Mode CHARGEMENT — palette_loadings = vérité ────
                ramasse_id_final = str(ramasse_id_to_update)

                # Lien palette_loadings pour les palettes scannées au
                # chargement (= palettes qui partent dans le camion).
                sscc_list = [p.sscc for p in basket]
                inserted = 0
                if sscc_list:
                    inserted, conflicts = await asyncio.to_thread(
                        link_palettes_to_ramasse,
                        tenant_id,
                        sscc_list=sscc_list,
                        ramasse_id=ramasse_id_final,
                        user_email=user_email,
                    )
                    if conflicts:
                        _log.warning(
                            "Définitif : %d palettes en conflict UNIQUE : %s",
                            len(conflicts), conflicts[:5],
                        )
                        ui.notify(
                            f"⚠ {len(conflicts)} palette(s) déjà liée(s) "
                            "ailleurs — vérifier le journal admin.",
                            type="warning", timeout=6000,
                        )

                # Rebuild depuis palette_loadings (= palettes scannées
                # accumulées sur la ramasse, J1 + J2 + tout).
                lines, total_cartons, total_palettes, total_poids = await asyncio.to_thread(
                    rebuild_lines_from_palettes, ramasse_id_final, tenant_id,
                )

                packaging_lines_ui = _get_packaging_lines()

                df_lines = _build_df_for_pdf(lines)
                pdf_bytes = await asyncio.to_thread(
                    build_bl_enlevements_pdf,
                    date_creation=today_paris(),
                    date_ramasse=d,
                    destinataire_title=dest_title,
                    destinataire_lines=dest_addr_lines,
                    df_lines=df_lines,
                    packaging_lines=packaging_lines_ui,
                    previous_lines=previous_lines_for_diff,  # snapshot prov pour diff
                    version=next_version,
                    kind="definitif",
                )

                result = await asyncio.to_thread(
                    update_ramasse,
                    ramasse_id_final,
                    date_ramasse=d,
                    destinataire=dest_title,
                    recipients=recipients,
                    lines=lines,
                    total_cartons=total_cartons,
                    total_palettes=total_palettes,
                    total_poids_kg=total_poids,
                    packaging=packaging_lines_ui,
                    pdf_bytes=pdf_bytes,
                    target_status="definitif",
                    tenant_id=tenant_id,
                )
                if result is None:
                    ui.notify(
                        "Transition refusée (legacy → définitif interdit).",
                        type="negative", timeout=8000,
                    )
                    return

            # ── Email + download + notif (commun aux 2 modes) ──
            subject = build_email_subject(d, kind=target_status)
            body = build_email_body(
                d, total_palettes=total_palettes, total_cartons=total_cartons,
                packaging_lines=packaging_lines_ui,
                kind=target_status,
            )
            kind_short = "Provisoire" if target_status == "previsionnel" else "Definitif"
            fname = f"BL_{kind_short}_{d:%Y%m%d}.pdf"
            await asyncio.to_thread(
                send_html_with_pdf,
                to_email=recipients, subject=subject,
                html_body=body, attachments=[(fname, pdf_bytes)],
            )

            if target_status == "definitif":
                # Le chauffeur a besoin du PDF papier — download obligatoire
                ui.download(pdf_bytes, fname)
            kind_label = (
                "Demande provisoire envoyée"
                if target_status == "previsionnel"
                else "BL définitif envoyé"
            )
            ui.notify(
                f"✓ {kind_label} — {total_palettes} palette(s) dans le BL.",
                type="positive", icon="check_circle", timeout=6000,
            )

            # Reset (panier + persistance)
            state["basket"] = []
            _clear_persisted_basket()
            _refresh_basket()
            _refresh_unscanned()
            _refresh_existing_ramasses()
            _refresh_linked_palettes()
            _refresh_state_banner()
            _refresh_cold_room()
        except Exception as exc:
            _log.exception("Erreur validation chargement camion (kind=%s)", target_status)
            ui.notify(f"Erreur : {exc}", type="negative", timeout=8000)
        finally:
            prev_btn.props(remove="loading")
            def_btn.props(remove="loading")
            _refresh_action_buttons()

    prev_btn.on_click(lambda _e: _on_validate("previsionnel"))
    def_btn.on_click(lambda _e: _on_validate("definitif"))

    async def _on_link_only():
        """Mode rattrapage : lie le panier à la ramasse sélectionnée sans
        envoyer d'email ni regénérer le PDF.

        Use case : le BL a déjà été envoyé via un autre canal (manuellement
        ou pour une ramasse legacy d'avant la refonte), on veut juste
        enregistrer la traçabilité physique des palettes scannées pour
        qu'elles disparaissent de la liste "non chargées".
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
            _refresh_linked_palettes()
            _refresh_state_banner()
            _refresh_cold_room()
        except Exception as exc:
            _log.exception("Erreur rattrapage link")
            ui.notify(f"Erreur : {exc}", type="negative")
        finally:
            link_only_btn.enable()
            link_only_btn.props(remove="loading")

    link_only_btn.on_click(_on_link_only)

    # Quand le select ramasse change → re-évaluer la visibilité du bouton rattrapage
    def _on_ramasse_select_changed(_e=None):
        _refresh_action_buttons()
        _refresh_linked_palettes()

    ramasse_to_update_select.on_value_change(_on_ramasse_select_changed)

    def _on_clear_basket():
        """Bouton manuel : vide le panier persisté + l'état courant."""
        state["basket"] = []
        _clear_persisted_basket()
        ui.notify("Panier vidé.", type="info", timeout=1500)
        _refresh_basket()

    clear_basket_btn.on_click(_on_clear_basket)

    # ── Bandeau d'état permanent ──
    # Cache de la ramasse active (None si aucune) — partagé entre
    # _refresh_state_banner et _try_auto_select_active_ramasse pour
    # éviter 2 lectures DB consécutives au refresh.
    active_ramasse_cache: dict = {"current": None}

    def _format_who_when(email: str | None, when) -> str:
        """Formate « par X le DD/MM HH:MM » (cohérence multi-user)."""
        who = email or "?"
        when_str = fmt_paris(when, "%d/%m %H:%M") if when else "?"
        return f"par {who} le {when_str}"

    def _render_state_banner():
        """Bandeau d'état permanent en haut de la page. 4 cas :

        - Pas de destinataire sélectionné → vide.
        - Aucune ramasse active pour ce dest → bandeau gris vert
          « Prêt à créer une nouvelle ramasse ».
        - Ramasse prévisionnelle en cours → bandeau bleu avec créateur,
          nb palettes, dernière update.
        - BL définitif envoyé → bandeau vert « en attente du chauffeur ».

        Lit ``active_ramasse_cache["current"]`` (populé par
        :func:`_refresh_state_banner`).
        """
        state_banner_container.clear()
        dest = (dest_select.value or "").strip()
        if not dest:
            return
        ramasse = active_ramasse_cache.get("current")

        if not ramasse:
            # Aucune ramasse active — l'opérateur peut en créer une
            with state_banner_container:
                with ui.row().classes(
                    "w-full items-center gap-3 q-pa-md",
                ).style(
                    "background: #F3F4F6; border-left: 6px solid #9CA3AF; "
                    "border-radius: 6px",
                ):
                    ui.icon("inbox", size="lg").style("color: #6B7280")
                    with ui.column().classes("flex-1 gap-0"):
                        ui.label("AUCUNE RAMASSE EN COURS").classes(
                            "text-subtitle1",
                        ).style(
                            "color: #374151; font-weight: 700; letter-spacing: 1px",
                        )
                        ui.label(
                            f"Prêt à démarrer une nouvelle ramasse pour {dest}. "
                            "Scanne une palette puis envoie le prévisionnel.",
                        ).classes("text-caption").style("color: #4B5563")
            return

        # Ramasse en cours
        status = str(ramasse.get("status") or "")
        is_def = (status == "definitif")
        bg, border, ink, sub, icon = (
            ("#ECFDF5", "#10B981", "#065F46", "#047857", "check_circle")
            if is_def
            else ("#EFF6FF", "#3B82F6", "#1E3A8A", "#1E40AF", "schedule_send")
        )
        title = "BL DÉFINITIF ENVOYÉ" if is_def else "RAMASSE PRÉVISIONNELLE EN COURS"
        sub_action = (
            "En attente du chauffeur. Marque comme livrée quand il sera passé."
            if is_def
            else "Continue à scanner pour ajouter des palettes, ou envoie le BL définitif."
        )
        dr = ramasse.get("date_ramasse")
        date_str = dr.strftime("%d/%m/%Y") if hasattr(dr, "strftime") else str(dr)
        nb_palettes = int(ramasse.get("total_palettes") or 0)
        version = int(ramasse.get("version") or 1)
        created_meta = _format_who_when(
            ramasse.get("created_by_email"), ramasse.get("created_at"),
        )
        updated_meta = (
            f"Dernière modification : {fmt_paris(ramasse.get('updated_at'), '%d/%m %H:%M')}"
            if ramasse.get("updated_at") else ""
        )

        with state_banner_container:
            with ui.row().classes(
                "w-full items-center gap-3 q-pa-md no-wrap",
            ).style(
                f"background: {bg}; border-left: 6px solid {border}; "
                "border-radius: 6px",
            ):
                ui.icon(icon, size="lg").style(f"color: {border}")
                with ui.column().classes("flex-1 gap-0"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(title).classes("text-subtitle1").style(
                            f"color: {ink}; font-weight: 700; letter-spacing: 1px",
                        )
                        if version > 1:
                            ui.badge(f"v{version}", color="orange-8").props("outline")
                    ui.label(
                        f"{dest} · ramasse du {date_str} · {nb_palettes} palette(s)",
                    ).classes("text-body2").style(
                        f"color: {ink}; font-weight: 500",
                    )
                    ui.label(f"Créée {created_meta}").classes("text-caption").style(
                        f"color: {sub}",
                    )
                    if updated_meta:
                        ui.label(updated_meta).classes("text-caption").style(
                            f"color: {sub}; opacity: 0.85",
                        )
                    ui.label(sub_action).classes("text-caption q-mt-xs").style(
                        f"color: {sub}; font-style: italic",
                    )

    def _refresh_state_banner():
        """Recharge la ramasse active depuis la DB puis ré-affiche le
        bandeau. À appeler à chaque changement de dest ou après une
        action qui change l'état (envoi, suppression, livraison)."""
        dest = (dest_select.value or "").strip()
        if not dest:
            active_ramasse_cache["current"] = None
        else:
            try:
                active_ramasse_cache["current"] = get_active_ramasse_for_dest(
                    dest, tenant_id=tenant_id,
                )
            except Exception:
                _log.warning("Échec get_active_ramasse_for_dest", exc_info=True)
                active_ramasse_cache["current"] = None
        _render_state_banner()

    def _try_auto_select_active_ramasse():
        """Si une ramasse active existe pour le dest courant ET que le
        panier est vide ET qu'aucune ramasse n'est sélectionnée → on la
        pré-sélectionne dans le select. Évite à Mohamed d'avoir à
        scroller pour retrouver « la ramasse de Max d'hier »."""
        if state["basket"]:
            return
        if ramasse_to_update_select.value:
            return
        ramasse = active_ramasse_cache.get("current")
        if not ramasse:
            return
        rid = str(ramasse["id"])
        # Vérifie que la ramasse est dans les options actuelles
        if rid not in ramasse_status_by_id:
            _refresh_existing_ramasses()
        if rid not in ramasse_status_by_id:
            _log.info(
                "Ramasse active %s hors liste limit=15, skip auto-select", rid,
            )
            return
        ramasse_to_update_select.value = rid
        _on_ramasse_select_changed()

    # Au changement de destinataire : libérer l'ancienne sélection,
    # recharger le bandeau d'état + retenter l'auto-sélection.
    def _on_dest_changed_for_state(_e=None):
        current_rid = ramasse_to_update_select.value
        if current_rid and current_rid in ramasse_status_by_id:
            ramasse_to_update_select.value = ""
        _refresh_state_banner()
        _refresh_cold_room()
        _try_auto_select_active_ramasse()

    dest_select.on_value_change(_on_dest_changed_for_state)

    # ── Historique court (5 dernières ramasses, sans corbeille) ──
    # Pour les actions complètes (corbeille, marquage chauffeur passé,
    # export CSV, etc.), un lien renvoie vers /historique-ramasses.
    _render_recent_history(tenant_id)

    # Initial render — affichera les palettes restaurées si présentes
    _refresh_basket()
    _refresh_cold_room()
    _refresh_state_banner()
    _try_auto_select_active_ramasse()


def _render_recent_history(tenant_id: str) -> None:
    """Petit bloc en bas de /chargement-camion : 5 dernières ramasses
    + lien vers la page d'historique complète. Lecture seule (pas
    d'actions inline — on évite la duplication avec /historique-ramasses).
    """
    try:
        items = list_ramasses(tenant_id=tenant_id, limit=5)
    except Exception:
        _log.warning("Échec recent history", exc_info=True)
        items = []

    with ui.expansion(
        text="Historique récent",
        icon="history",
        value=False,
    ).classes("w-full q-mt-lg").props(
        "dense header-class='text-subtitle2'",
    ).style(
        f"border: 1px solid {COLORS['border']}; border-radius: 8px",
    ):
        with ui.row().classes("w-full items-center justify-between q-pa-sm"):
            ui.label("5 dernières ramasses").classes("text-caption").style(
                f"color: {COLORS['ink2']}",
            )
            ui.button(
                "Voir tout l'historique",
                icon="open_in_new",
                on_click=lambda: ui.navigate.to("/historique-ramasses"),
            ).props("flat dense color=blue-7 size=sm")

        if not items:
            ui.label("Aucune ramasse pour l'instant.").classes(
                "text-grey-6 q-pa-sm",
            )
            return

        for r in items:
            dr = r.get("date_ramasse")
            date_str = dr.strftime("%d/%m/%Y") if hasattr(dr, "strftime") else str(dr)
            status = str(r.get("status") or "")
            badge_text = {
                "previsionnel": "PRÉV",
                "definitif": "DÉF",
                "legacy": "LEGACY",
                "sent": "LEGACY",
            }.get(status, status[:5].upper() if status else "?")
            badge_color = {
                "previsionnel": "blue-7",
                "definitif": "green-8",
            }.get(status, "grey-6")
            with ui.row().classes(
                "w-full items-center gap-3 q-px-sm q-py-xs",
            ).style(f"border-top: 1px solid {COLORS['border']}"):
                ui.badge(badge_text, color=badge_color).props("outline").style(
                    "font-size: 10px; min-width: 56px; text-align: center",
                )
                with ui.column().classes("flex-1 gap-0"):
                    ui.label(
                        f"{date_str} — {r.get('destinataire', '?')}",
                    ).classes("text-body2").style(
                        f"color: {COLORS['ink']}; font-weight: 500",
                    )
                    livre_str = " · livrée" if r.get("driver_passed") else ""
                    ui.label(
                        f"{r.get('total_palettes', 0)} pal · "
                        f"{r.get('total_cartons', 0)} cartons{livre_str}",
                    ).classes("text-caption").style(f"color: {COLORS['ink2']}")


# ─── Helpers ────────────────────────────────────────────────────────────────

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
