"""
pages/ramasse.py
=============
Page Fiche de ramasse — NiceGUI + Quasar Table.

Réutilise toute la logique métier de common/ramasse.py et common/easybeer.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import os

import requests
from nicegui import ui

_log = logging.getLogger("ferment.ramasse")

from common.auth import validate_email
from common.easybeer import (
    EasyBeerError,
    fetch_carton_weights,
    get_brassins_archives,
    get_brassins_en_cours,
    get_code_barre_matrice,
    get_warehouses,
)
from common.easybeer import (
    is_configured as eb_configured,
)
from common.email import EmailSendError, send_html_with_pdf
from common.ramasse import (
    PALETTE_EMPTY_WEIGHT,
    build_packaging_summary,
    build_ramasse_lines,
    clean_product_label,
    load_destinataires,
    load_packaging_items,
    parse_barcode_matrix,
    today_paris,
)
from common.ramasse_history import (
    count_ramasses,
    get_ramasse,
    list_ramasses,
    mark_driver_passed,
    save_ramasse,
    update_ramasse,
)
from common.xlsx_fill import build_bl_enlevements_pdf
from pages.auth import require_auth
from pages.theme import COLORS, confirm_dialog, page_layout, section_title

# ─── Helpers ────────────────────────────────────────────────────────────────

def _load_brassins() -> tuple[list[dict], list[str]]:
    """Charge brassins en cours + 3 derniers archivés. Retourne (brassins, erreurs)."""
    errors: list[str] = []

    try:
        en_cours = get_brassins_en_cours()
    except (EasyBeerError, requests.RequestException) as exc:
        errors.append(f"Brassins en cours : {exc}")
        en_cours = []

    en_cours_ids = {b.get("idBrassin") for b in en_cours}
    try:
        archives = get_brassins_archives(nombre=3)
        for b in archives:
            if b.get("idBrassin") not in en_cours_ids:
                b["_is_archive"] = True
                en_cours.append(b)
    except (EasyBeerError, requests.RequestException) as exc:
        errors.append(f"Brassins archivés : {exc}")
    return [b for b in en_cours if not b.get("annule")], errors


def _load_cb_matrix() -> dict[int, list[dict]] | None:
    try:
        raw = get_code_barre_matrice()
        return parse_barcode_matrix(raw)
    except (EasyBeerError, requests.RequestException):
        _log.warning("Impossible de charger la matrice codes-barres", exc_info=True)
        return None


def _load_eb_weights() -> dict[tuple[int, str], float] | None:
    try:
        return fetch_carton_weights()
    except (EasyBeerError, requests.RequestException):
        _log.warning("Impossible de charger les poids cartons", exc_info=True)
        return None


def _load_entrepot() -> int | None:
    try:
        warehouses = get_warehouses()
        for w in warehouses:
            if w.get("principal"):
                return w.get("idEntrepot")
        return warehouses[0].get("idEntrepot") if warehouses else None
    except (EasyBeerError, requests.RequestException):
        _log.warning("Impossible de charger les entrepots", exc_info=True)
        return None


def _brassin_label(b: dict) -> str:
    nom = b.get("nom", "?")
    prod = clean_product_label((b.get("produit") or {}).get("libelle", "?"))
    vol = b.get("volume", 0)
    tag = " [archivé]" if b.get("_is_archive") else ""
    return f"{nom} — {prod} — {vol:.0f}L{tag}"


def _compute_row(row: dict, meta: dict) -> dict:
    """Calcule palettes et poids pour une ligne."""
    cartons = int(row.get("cartons") or 0)
    pc = float(meta.get("_poids_carton", 0))
    pal_cap = int(meta.get("_palette_capacity", 0))
    nb_pal = math.ceil(cartons / pal_cap) if pal_cap > 0 and cartons > 0 else 0
    poids = int(round(cartons * pc + nb_pal * PALETTE_EMPTY_WEIGHT, 0))
    return {**row, "palettes": nb_pal, "poids": poids}


# ─── Colonnes Quasar Table ──────────────────────────────────────────────────

TABLE_COLUMNS = [
    {"name": "ref",           "label": "Réf.",                    "field": "ref",           "sortable": True,  "align": "left"},
    {"name": "produit",       "label": "Produit (goût + format)", "field": "produit",       "sortable": True,  "align": "left"},
    {"name": "ddm",           "label": "DDM",                     "field": "ddm",           "sortable": False, "align": "center"},
    {"name": "cartons",       "label": "Cartons",                 "field": "cartons",       "sortable": True,  "align": "right"},
    {"name": "palettes",      "label": "Palettes",                "field": "palettes",      "sortable": False, "align": "right"},
    {"name": "poids_display", "label": "Poids (kg)",              "field": "poids_display", "sortable": False, "align": "right"},
]


# ─── Page ───────────────────────────────────────────────────────────────────

@ui.page("/ramasse", response_timeout=15.0)
async def page_ramasse():
    user = require_auth()
    if not user:
        return

    with page_layout("Fiche de ramasse", "local_shipping", "/ramasse") as sidebar:

        # ── Guards ───────────────────────────────────────────────────
        if not eb_configured():
            ui.label("EasyBeer non configuré.").classes("text-negative")
            ui.label(
                f"EASYBEER_API_USER={'OK' if os.environ.get('EASYBEER_API_USER') else 'manquant'}, "
                f"EASYBEER_API_PASS={'OK' if os.environ.get('EASYBEER_API_PASS') else 'manquant'}"
            ).classes("text-caption text-grey-6")
            return

        # ── Chargement données (dans un thread pour ne pas bloquer l'event loop) ──
        def _load_all_eb_data():
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=4) as pool:
                f_brassins = pool.submit(_load_brassins)
                f_cb = pool.submit(_load_cb_matrix)
                f_entrepot = pool.submit(_load_entrepot)
                f_weights = pool.submit(_load_eb_weights)
            return (
                f_brassins.result(),
                f_cb.result(),
                f_entrepot.result(),
                f_weights.result(),
            )

        (brassins, load_errors), cb_by_product, id_entrepot, eb_weights = (
            await asyncio.to_thread(_load_all_eb_data)
        )

        destinataires = load_destinataires()
        dest_names = [d["name"] for d in destinataires] if destinataires else ["SOFRIPA"]

        if load_errors:
            with ui.row().classes("w-full items-center gap-2 q-pa-sm").style(
                "background: #FFF3CD; border-radius: 8px; border: 1px solid #FFEAA7"
            ):
                ui.icon("warning", color="orange-8", size="sm")
                with ui.column().classes("gap-0"):
                    for err in load_errors:
                        ui.label(f"{err}").classes("text-caption text-orange-9")

        if not brassins:
            ui.label("Aucun brassin disponible dans EasyBeer.").classes("text-grey-6")
            return

        # ── Sidebar : vide (bouton recharger déplacé à droite) ─────────
        with sidebar:
            pass

        # ── État d'édition (mode mise à jour d'une ramasse existante) ──
        # editing_id: None = nouvelle ramasse, sinon UUID de la ramasse éditée
        # current_version: version courante (1 = nouveau BL, 2+ = mise à jour)
        # previous_lines: snapshot des lignes de la version précédente (pour diff PDF)
        # pending_cartons: cartons à restaurer au prochain rebuild du tableau (par ref)
        edit_state: dict = {
            "editing_id": None,
            "current_version": 1,
            "previous_lines": None,
            "pending_cartons": {},
            "pending_packaging": {},
        }

        # ── Bandeau "Mode édition" (conditionnel, affiché en haut) ─────
        edit_banner_container = ui.row().classes("w-full")

        def _render_edit_banner():
            edit_banner_container.clear()
            if edit_state["editing_id"] is None:
                return
            next_version = int(edit_state["current_version"]) + 1
            with edit_banner_container:
                with ui.row().classes("w-full items-center gap-3 q-pa-sm").style(
                    "background: #FFF3CD; border: 1px solid #FFB74D; border-radius: 8px"
                ):
                    ui.icon("edit_note", color="orange-9", size="md")
                    with ui.column().classes("flex-1 gap-0"):
                        ui.label(
                            f"Mode édition — vous modifiez une ramasse existante (v{edit_state['current_version']} → v{next_version})"
                        ).classes("text-subtitle2").style("color: #7C2D12; font-weight: 600")
                        ui.label(
                            "Les modifications apparaîtront dans le PDF renvoyé (nouvelles lignes en jaune, modifiées en bleu)."
                        ).classes("text-caption").style("color: #92400E")
                    ui.button(
                        "Annuler l'édition",
                        icon="close",
                        on_click=lambda: _cancel_edit(),
                    ).props("flat color=orange-9 dense")

        def _cancel_edit():
            """Sort du mode édition et vide le formulaire."""
            edit_state["editing_id"] = None
            edit_state["current_version"] = 1
            edit_state["previous_lines"] = None
            edit_state["pending_cartons"] = {}
            edit_state["pending_packaging"] = {}
            brassin_select.value = []
            _render_edit_banner()
            on_brassins_changed()
            ui.notify("Mode édition annulé.", type="info")

        # ── Sélection brassins + recharger ────────────────────────────
        with ui.row().classes("w-full items-center gap-4"):
            with ui.column().classes("flex-1 gap-0"):
                section_title("Sélection des brassins", "playlist_add_check")

        brassin_options = {
            b["idBrassin"]: _brassin_label(b)
            for b in brassins
        }

        with ui.row().classes("w-full items-end gap-3"):
            brassin_select = ui.select(
                brassin_options,
                multiple=True,
                value=[],
                label="Brassins à inclure",
            ).classes("flex-1").props("outlined use-chips")

            def do_reload():
                ui.navigate.to("/ramasse")

            ui.button(icon="refresh", on_click=do_reload).props(
                'flat round color=grey-7 aria-label="Recharger les données EasyBeer"'
            ).tooltip("Recharger les données EasyBeer")

        # ── Paramètres : date + destinataire (dans le contenu) ────────
        with ui.row().classes("w-full items-end gap-4 q-mt-sm"):
            date_ramasse = ui.input(
                "Date de ramasse",
                value=today_paris().strftime("%d/%m/%Y"),
            ).props('outlined dense').style("max-width: 200px")
            # Popup calendrier Quasar attaché à l'input
            with date_ramasse.add_slot("append"):
                ui.icon("event").classes("cursor-pointer").props("name=event")
                with ui.menu().props("anchor='bottom right' self='top right'"):
                    date_picker = ui.date(
                        value=today_paris().isoformat(),
                    ).props('first-day-of-week=1 minimal')

                    def _sync_date(e=None):
                        v = date_picker.value
                        if v:
                            d = dt.date.fromisoformat(v)
                            date_ramasse.value = d.strftime("%d/%m/%Y")
                    date_picker.on_value_change(_sync_date)

            dest_select = ui.select(
                dest_names,
                value=dest_names[0],
                label="Destinataire",
            ).props("outlined dense").style("min-width: 200px")

        # ── Conteneur dynamique ──────────────────────────────────────
        content_container = ui.column().classes("w-full gap-5 q-mt-md")

        # ── Refs partagées ───────────────────────────────────────────
        table_ref = {"table": None, "rows": []}
        kpi_labels = {"cartons": None, "palettes": None, "poids": None}

        def _update_kpis():
            """Met à jour les KPI depuis table_ref['rows']."""
            active = [r for r in table_ref["rows"] if int(r.get("cartons") or 0) > 0]
            tot_c = sum(int(r.get("cartons") or 0) for r in active)
            tot_p = sum(int(r.get("palettes") or 0) for r in active)
            tot_w = sum(int(r.get("poids") or 0) for r in active)
            if kpi_labels["cartons"]:
                kpi_labels["cartons"].text = f"{tot_c:,}".replace(",", " ")
            if kpi_labels["palettes"]:
                kpi_labels["palettes"].text = str(tot_p)
            if kpi_labels["poids"]:
                kpi_labels["poids"].text = f"{tot_w:,}".replace(",", " ")

        def on_brassins_changed(e=None):
            """Reconstruit le tableau quand la sélection change."""
            # ── Sauvegarder les cartons saisis avant de tout reconstruire ──
            saved_cartons: dict[str, int] = {}
            for row in table_ref["rows"]:
                c = row.get("cartons")
                if c is not None and c > 0:
                    saved_cartons[row["ref"]] = int(c)

            # Les cartons "pending" (mode édition) sont fusionnés dans saved_cartons.
            # Ils écrasent uniquement les refs qu'ils définissent — les autres saisies
            # en cours sont préservées. Consommés une seule fois puis vidés.
            if edit_state.get("pending_cartons"):
                saved_cartons.update(edit_state["pending_cartons"])
                edit_state["pending_cartons"] = {}

            content_container.clear()
            table_ref["table"] = None
            table_ref["rows"] = []

            selected_ids = brassin_select.value or []
            selected_ids_set = {int(x) for x in selected_ids if x is not None}
            selected = [b for b in brassins if b["idBrassin"] in selected_ids_set]

            if not selected:
                with content_container:
                    ui.label(
                        "Sélectionne au moins un brassin pour construire la fiche."
                    ).classes("text-grey-6 text-body1 q-pa-md")
                return

            try:
                rows, meta_by_label = build_ramasse_lines(
                    selected, id_entrepot, cb_by_product, eb_weights
                )
            except (ValueError, KeyError, TypeError) as exc:
                with content_container:
                    ui.label(f"Erreur lors du chargement des lignes : {exc}").classes(
                        "text-negative text-body1 q-pa-md"
                    )
                return

            if not rows:
                with content_container:
                    ui.label(
                        "Aucune ligne de produit trouvée pour ces brassins."
                    ).classes("text-grey-6 text-body1 q-pa-md")
                    if cb_by_product is None:
                        ui.label(
                            "La matrice codes-barres EasyBeer n'a pas pu être chargée. "
                            "Vérifie la connexion à l'API."
                        ).classes("text-negative text-caption q-pa-sm")
                return

            # ── Préparer les données ──────────────────────────────
            grid_rows = []
            for r in rows:
                label = r["Produit (goût + format)"]
                meta = meta_by_label.get(label, {})
                # Extraire le goût depuis le label "Kéfir Original — 12x33cl"
                gout = label.split(" — ")[0].strip() if " — " in label else label
                grid_row = {
                    "ref": r["Référence"],
                    "produit": label,
                    "_gout": gout,
                    "ddm": r["DDM"].strftime("%d/%m/%Y") if hasattr(r["DDM"], "strftime") else str(r["DDM"]),
                    "cartons": None,
                    "poids_u": float(meta.get("_poids_carton", 0)),
                    "pal_cap": int(meta.get("_palette_capacity", 0)),
                    "palettes": 0,
                    "poids": 0,
                    "poids_display": "—",
                }
                grid_rows.append(grid_row)

            # Trier par goût pour regrouper visuellement
            grid_rows.sort(key=lambda r: r["_gout"])

            # ── Restaurer les cartons saisis précédemment ──────────
            for grid_row in grid_rows:
                ref = grid_row["ref"]
                if ref in saved_cartons:
                    c = saved_cartons[ref]
                    grid_row["cartons"] = c
                    cap = int(grid_row.get("pal_cap") or 0)
                    pu = float(grid_row.get("poids_u") or 0)
                    pal = math.ceil(c / cap) if cap > 0 and c > 0 else 0
                    grid_row["palettes"] = pal
                    p = int(round(c * pu + pal * PALETTE_EMPTY_WEIGHT))
                    grid_row["poids"] = p
                    grid_row["poids_display"] = f"{p:,} kg".replace(",", " ") if p else "—"

            # ── Insérer des en-têtes par goût ──────────────────────
            ordered_rows: list[dict] = []
            current_gout = None
            for grid_row in grid_rows:
                if grid_row["_gout"] != current_gout:
                    current_gout = grid_row["_gout"]
                    ordered_rows.append({
                        "_sep": True,
                        "_gout": current_gout,
                        "ref": f"_sep_{current_gout}",
                        "produit": "", "ddm": "",
                        "cartons": None, "palettes": 0,
                        "poids": 0, "poids_display": "",
                        "poids_u": 0, "pal_cap": 0,
                    })
                ordered_rows.append(grid_row)

            table_ref["rows"] = grid_rows  # rows sans séparateurs (pour calculs)

            try:
              with content_container:
                # ── KPIs ─────────────────────────────────────────────
                active = [r for r in grid_rows if (r["cartons"] or 0) > 0]
                tot_c = sum(int(r["cartons"] or 0) for r in active)
                tot_p = sum(int(r["palettes"] or 0) for r in active)
                tot_w = sum(int(r["poids"] or 0) for r in active)

                with ui.row().classes("w-full gap-4"):
                    with ui.card().classes("kpi-card q-pa-none flex-1").props("flat"):
                        with ui.card_section().classes("row items-center gap-3 q-pa-md"):
                            with ui.element("div").classes("q-pa-xs").style(
                                f"background: {COLORS['green']}10; border-radius: 6px"
                            ):
                                ui.icon("inventory_2", size="sm").style(f"color: {COLORS['green']}")
                            with ui.column().classes("gap-0"):
                                ui.label("Total cartons").classes("text-caption").style(
                                    f"color: {COLORS['ink2']}; font-weight: 500"
                                )
                                kpi_labels["cartons"] = ui.label(
                                    f"{tot_c:,}".replace(",", " ")
                                ).classes("text-h6").style(
                                    f"color: {COLORS['ink']}; font-weight: 600"
                                ).props('aria-live="polite"')

                    with ui.card().classes("kpi-card q-pa-none flex-1").props("flat"):
                        with ui.card_section().classes("row items-center gap-3 q-pa-md"):
                            with ui.element("div").classes("q-pa-xs").style(
                                f"background: {COLORS['orange']}10; border-radius: 6px"
                            ):
                                ui.icon("view_in_ar", size="sm").style(f"color: {COLORS['orange']}")
                            with ui.column().classes("gap-0"):
                                ui.label("Total palettes").classes("text-caption").style(
                                    f"color: {COLORS['ink2']}; font-weight: 500"
                                )
                                kpi_labels["palettes"] = ui.label(str(tot_p)).classes("text-h6").style(
                                    f"color: {COLORS['ink']}; font-weight: 600"
                                ).props('aria-live="polite"')

                    with ui.card().classes("kpi-card q-pa-none flex-1").props("flat"):
                        with ui.card_section().classes("row items-center gap-3 q-pa-md"):
                            with ui.element("div").classes("q-pa-xs").style(
                                f"background: {COLORS['blue']}10; border-radius: 6px"
                            ):
                                ui.icon("scale", size="sm").style(f"color: {COLORS['blue']}")
                            with ui.column().classes("gap-0"):
                                ui.label("Poids total (kg)").classes("text-caption").style(
                                    f"color: {COLORS['ink2']}; font-weight: 500"
                                )
                                kpi_labels["poids"] = ui.label(
                                    f"{tot_w:,}".replace(",", " ")
                                ).classes("text-h6").style(
                                    f"color: {COLORS['ink']}; font-weight: 600"
                                ).props('aria-live="polite"')

                # ── Tableau Quasar ─────────────────────────────────
                section_title("Détail produits", "table_chart")

                ui.label(
                    "Saisis le nombre de cartons — palettes et poids se calculent automatiquement. "
                    "Clique sur le nombre de palettes pour l'ajuster manuellement."
                ).classes("text-caption text-grey-6 q-mb-xs")

                nb_cols = len(TABLE_COLUMNS)

                table = ui.table(
                    columns=TABLE_COLUMNS,
                    rows=ordered_rows,
                    row_key="ref",
                    pagination={"rowsPerPage": 0},
                ).classes("w-full").style(
                    f"color: {COLORS['ink']}"
                )
                table.props("flat bordered dense")
                table_ref["table"] = table

                # Slot body — en-tête goût OU ligne de données
                ORANGE = COLORS["orange"]
                GREEN = COLORS["green"]
                table.add_slot("body", r'''
                    <q-tr v-if="props.row._sep" :props="props">
                        <q-td colspan="''' + str(nb_cols) + r'''"
                               style="background: ''' + GREEN + r'''10; padding: 8px 12px; font-weight: 600; font-size: 13px; border-bottom: 2px solid ''' + GREEN + r'''30;">
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <q-icon name="local_drink" size="18px" style="color: ''' + GREEN + r'''" />
                                <span style="color: ''' + GREEN + r'''">{{ props.row._gout }}</span>
                            </div>
                        </q-td>
                    </q-tr>
                    <q-tr v-else :props="props"
                          :style="props.row.cartons == null || props.row.cartons == 0 ? 'opacity: 0.45' : ''">
                        <q-td v-for="col in props.cols" :key="col.name" :props="props"
                              :style="'text-align: ' + col.align">
                            <template v-if="col.name === 'cartons'">
                                <q-input
                                    v-model.number="props.row.cartons"
                                    type="number"
                                    dense
                                    borderless
                                    placeholder="0"
                                    input-class="text-right text-bold"
                                    :input-style="{color: props.row.cartons != null && props.row.cartons != 0 ? '#111827' : '#9CA3AF'}"
                                    style="max-width: 80px"
                                    @change="() => $parent.$emit('cartons_changed', {ref: props.row.ref, cartons: props.row.cartons})"
                                />
                            </template>
                            <template v-else-if="col.name === 'produit'">
                                <span :style="props.row.cartons != null && props.row.cartons > 0 ? 'font-weight: 600' : ''">
                                    {{ props.row[col.field] }}
                                </span>
                            </template>
                            <template v-else-if="col.name === 'palettes'">
                                <span :style="{
                                    color: '''' + ORANGE + r'''',
                                    fontWeight: 600,
                                    cursor: 'pointer',
                                }">
                                    {{ props.row.palettes || 0 }}
                                    <q-icon name="edit" size="12px" color="grey-5" class="q-ml-xs" />
                                </span>
                                <q-popup-edit v-model="props.row.palettes" v-slot="scope"
                                    @update:model-value="() => $parent.$emit('palettes_changed', {ref: props.row.ref, palettes: props.row.palettes})">
                                    <q-input v-model.number="scope.value" type="number" dense autofocus
                                        placeholder="0" min="0"
                                        input-class="text-right text-bold"
                                        style="min-width: 80px"
                                        hint="Entrée pour valider"
                                        @keyup.enter="scope.set" />
                                </q-popup-edit>
                            </template>
                            <template v-else>
                                {{ props.row[col.field] }}
                            </template>
                        </q-td>
                    </q-tr>
                ''')

                # Helper : reconstruit les rows avec séparateurs pour le tableau
                def _rebuild_table_rows():
                    """Reconstruit ordered_rows à partir de table_ref['rows'] (données seules)."""
                    out: list[dict] = []
                    cur_gout = None
                    for row in table_ref["rows"]:
                        g = row.get("_gout", "")
                        if g != cur_gout:
                            cur_gout = g
                            out.append({
                                "_sep": True, "_gout": cur_gout,
                                "ref": f"_sep_{cur_gout}",
                                "produit": "", "ddm": "",
                                "cartons": None, "palettes": 0,
                                "poids": 0, "poids_display": "",
                                "poids_u": 0, "pal_cap": 0,
                            })
                        out.append(row)
                    table.rows[:] = out
                    table.update()

                # Handler @change : sync + recalcul automatique
                def on_cartons_changed(e):
                    data = e.args
                    ref = data.get("ref")
                    c = data.get("cartons")
                    try:
                        c = int(float(c)) if c is not None and c != "" else 0
                    except (TypeError, ValueError):
                        c = 0
                    if c < 0:
                        c = 0

                    for row in table_ref["rows"]:
                        if row["ref"] == ref:
                            row["cartons"] = c
                            cap = int(row.get("pal_cap") or 0)
                            pu = float(row.get("poids_u") or 0)
                            pal = math.ceil(c / cap) if cap > 0 and c > 0 else 0
                            row["palettes"] = pal
                            p = int(round(c * pu + pal * PALETTE_EMPTY_WEIGHT))
                            row["poids"] = p
                            row["poids_display"] = f"{p:,} kg".replace(",", " ") if p else "—"
                            break

                    _rebuild_table_rows()
                    _update_kpis()

                table.on("cartons_changed", on_cartons_changed)

                def on_palettes_changed(e):
                    data = e.args
                    ref = data.get("ref")
                    p = data.get("palettes")
                    try:
                        p = int(float(p)) if p is not None and p != "" else 0
                    except (TypeError, ValueError):
                        p = 0
                    if p < 0:
                        p = 0

                    for row in table_ref["rows"]:
                        if row["ref"] == ref:
                            row["palettes"] = p
                            # Recalculer le poids avec le nouveau nombre de palettes
                            c = int(row.get("cartons") or 0)
                            pu = float(row.get("poids_u") or 0)
                            w = int(round(c * pu + p * PALETTE_EMPTY_WEIGHT))
                            row["poids"] = w
                            row["poids_display"] = f"{w:,} kg".replace(",", " ") if w else "—"
                            break

                    _rebuild_table_rows()
                    _update_kpis()

                table.on("palettes_changed", on_palettes_changed)

                # ── Emballages à récupérer ─────────────────────────
                packaging_state: dict = {"items": []}

                def _build_packaging_section():
                    """Construit la section emballages pour le destinataire courant."""
                    # Sauvegarder les qty saisies avant reset (mêmes principes que saved_cartons)
                    saved_pkg_qty: dict[str, int] = {
                        str(it.get("label") or ""): int(it.get("qty") or 0)
                        for it in packaging_state["items"]
                        if int(it.get("qty") or 0) > 0
                    }
                    # Fusionner avec les valeurs pending du mode édition (consommées une fois)
                    if edit_state.get("pending_packaging"):
                        saved_pkg_qty.update(edit_state["pending_packaging"])
                        edit_state["pending_packaging"] = {}

                    packaging_state["items"] = []
                    pkg_items = load_packaging_items(dest_select.value)
                    if not pkg_items:
                        return

                    # En mode édition, ouvrir l'expansion par défaut si des emballages sont restaurés
                    expansion_opened = bool(saved_pkg_qty)

                    section_title("Emballages à ramener", "inventory_2")
                    with ui.expansion(
                        "Demander des palettes d'emballage",
                        icon="move_to_inbox",
                        value=expansion_opened,
                    ).classes("w-full").props(
                        "dense header-class='text-subtitle2'"
                    ):
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
                                ui.label(item.get("unit", "palette")).classes(
                                    "text-caption text-grey-6"
                                )

                                def _on_qty(e, state=item_state, inp=qty_input):
                                    state["qty"] = int(inp.value or 0)

                                qty_input.on("update:model-value", _on_qty)

                packaging_container = ui.column().classes("w-full")
                with packaging_container:
                    _build_packaging_section()

                def _refresh_packaging(e=None):
                    """Reconstruit la section emballages quand le destinataire change."""
                    packaging_container.clear()
                    with packaging_container:
                        _build_packaging_section()

                dest_select.on_value_change(_refresh_packaging)

                def _get_packaging_lines() -> list[dict] | None:
                    """Retourne les emballages sélectionnés ou None."""
                    summary = build_packaging_summary(packaging_state["items"])
                    return summary if summary else None

                # ── Actions : PDF + Email ────────────────────────────
                section_title("Export et envoi", "send")

                def _get_dest_obj():
                    """Résout le destinataire sélectionné au moment de l'appel (pas au build)."""
                    return next((d for d in destinataires if d["name"] == dest_select.value), None)

                _init_dest = _get_dest_obj()
                default_emails = ", ".join(_init_dest.get("email_recipients", [])) if _init_dest else ""

                email_input = ui.input(
                    "Destinataires email",
                    value=default_emails,
                ).classes("w-full").props("outlined dense")

                def _on_dest_changed(e=None):
                    """Met à jour les emails quand le destinataire change."""
                    d = _get_dest_obj()
                    email_input.value = ", ".join(d.get("email_recipients", [])) if d else ""

                dest_select.on_value_change(_on_dest_changed)

                sender = os.environ.get("EMAIL_SENDER", "")
                if sender:
                    ui.label(f"Expéditeur : {sender}").classes("text-caption text-grey-6")

                with ui.row().classes("w-full gap-3 q-mt-sm"):
                    def _get_date_ramasse() -> dt.date:
                        """Parse la date depuis l'input ou le picker."""
                        # Essayer l'input text (format dd/mm/yyyy)
                        raw = date_ramasse.value or ""
                        try:
                            return dt.datetime.strptime(raw, "%d/%m/%Y").date()
                        except ValueError:
                            pass
                        # Fallback sur le date picker (format yyyy-mm-dd)
                        try:
                            return dt.date.fromisoformat(date_picker.value)
                        except (ValueError, AttributeError):
                            pass
                        return today_paris()

                    def _build_df_export(active_rows, d):
                        """Construit le DataFrame prêt pour build_bl_enlevements_pdf."""
                        import pandas as pd
                        df_export = pd.DataFrame([{
                            "Référence": r["ref"],
                            "Produit (goût + format)": r["produit"],
                            "DDM": r["ddm"],
                            "Date ramasse souhaitée": d.strftime("%d/%m/%Y"),
                            "Quantité cartons": int(r["cartons"]),
                            "Quantité palettes": int(r["palettes"]),
                            "Poids palettes (kg)": int(r["poids"]),
                        } for r in active_rows])
                        cols = ["Référence", "Produit (goût + format)", "DDM",
                                "Date ramasse souhaitée", "Quantité cartons",
                                "Quantité palettes", "Poids palettes (kg)"]
                        return df_export[cols]

                    def _build_pdf_for_active_rows(active_rows, d):
                        """Génère le PDF (v1 normal ou v2+ différentiel selon edit_state)."""
                        dest_title = dest_select.value
                        _dest = _get_dest_obj()
                        dest_lines = _dest.get("address_lines", []) if _dest else []

                        # Mode édition : passer previous_lines + version incrémentée
                        previous_lines_for_pdf = None
                        next_version = 1
                        if edit_state["editing_id"] is not None:
                            previous_lines_for_pdf = edit_state.get("previous_lines")
                            next_version = int(edit_state["current_version"]) + 1

                        return build_bl_enlevements_pdf(
                            date_creation=today_paris(),
                            date_ramasse=d,
                            destinataire_title=dest_title,
                            destinataire_lines=dest_lines,
                            df_lines=_build_df_export(active_rows, d),
                            packaging_lines=_get_packaging_lines(),
                            previous_lines=previous_lines_for_pdf,
                            version=next_version,
                        )

                    def do_download_pdf():
                        row_data = table_ref["rows"]
                        active_rows = [r for r in row_data if int(r.get("cartons") or 0) > 0]
                        if not active_rows:
                            ui.notify("Aucun carton renseigné.", type="warning")
                            return
                        try:
                            d = _get_date_ramasse()
                            pdf_bytes = _build_pdf_for_active_rows(active_rows, d)
                            ui.download(pdf_bytes, f"Fiche_de_ramasse_{d:%Y-%m-%d}.pdf")
                            ui.notify("PDF généré !", type="positive", icon="check")
                        except (OSError, ValueError, KeyError) as exc:
                            ui.notify(f"Erreur PDF : {exc}", type="negative")

                    ui.button(
                        "Télécharger PDF",
                        icon="picture_as_pdf",
                        on_click=do_download_pdf,
                    ).classes("flex-1").props("outline color=green-8")

                    async def do_send_email():
                        emails_raw = email_input.value or ""
                        to_list = [e.strip() for e in emails_raw.split(",") if e.strip()]
                        if not to_list:
                            ui.notify("Indique au moins un destinataire.", type="warning")
                            return
                        # Valider chaque email avant envoi
                        for addr in to_list:
                            try:
                                validate_email(addr)
                            except ValueError:
                                ui.notify(f"Adresse email invalide : {addr}", type="negative")
                                return

                        row_data = table_ref["rows"]
                        active_rows = [r for r in row_data if int(r.get("cartons") or 0) > 0]
                        if not active_rows:
                            ui.notify("Aucun carton renseigné.", type="warning")
                            return

                        send_btn_ref.disable()
                        try:
                            d = _get_date_ramasse()
                            is_update = edit_state["editing_id"] is not None
                            next_version = int(edit_state["current_version"]) + 1 if is_update else 1

                            pdf_bytes = _build_pdf_for_active_rows(active_rows, d)

                            dest_title = dest_select.value
                            tot_palettes = sum(int(r["palettes"]) for r in active_rows)
                            tot_cartons = sum(int(r["cartons"]) for r in active_rows)
                            tot_poids = sum(int(r["poids"]) for r in active_rows)
                            filename = f"Fiche_de_ramasse_{d:%Y%m%d}.pdf"

                            # ── Sujet + corps email : v1 ou v2+ ──
                            if is_update:
                                subject = (
                                    f"Demande de ramasse — {d:%d/%m/%Y} — Ferment Station "
                                    f"(MISE À JOUR v{next_version})"
                                )
                            else:
                                subject = f"Demande de ramasse — {d:%d/%m/%Y} — Ferment Station"

                            pkg_html = ""
                            _pkg_lines_email = _get_packaging_lines()
                            if _pkg_lines_email:
                                pkg_items_html = "<br>".join(
                                    f"— {p['qty']} {p['unit']}(s) {p['label']}"
                                    for p in _pkg_lines_email
                                )
                                pkg_html = (
                                    f"<p><strong>Emballages à ramener :</strong><br>"
                                    f"{pkg_items_html}</p>"
                                )

                            if is_update:
                                # Corps v2+ : mention explicite de la mise à jour
                                body = f"""
                                <p>Bonjour,</p>
                                <p>Nous vous adressons une <strong>version mise à jour</strong>
                                de notre demande de ramasse pour le <strong>{d:%d/%m/%Y}</strong>
                                (version {next_version}).</p>
                                <p>Total actuel : <strong>{tot_palettes}</strong>
                                palette{'s' if tot_palettes != 1 else ''}
                                ({tot_cartons} cartons).</p>
                                <p>Le PDF ci-joint fait apparaître les modifications :
                                <strong>nouvelles lignes en jaune</strong>,
                                <strong>lignes modifiées en bleu</strong>
                                (avec l'ancien nombre de cartons indiqué).</p>
                                {pkg_html}
                                <p>Merci,<br>Bon après-midi.</p>
                                <hr>
                                <p><strong>Ferment Station</strong><br>
                                Producteur de boissons fermentées<br>
                                26 Rue Robert Witchitz – 94200 Ivry-sur-Seine</p>
                                """
                            else:
                                body = f"""
                                <p>Bonjour,</p>
                                <p>Nous aurions besoin d'une ramasse pour le {d:%d/%m/%Y}.<br>
                                Pour <strong>{tot_palettes}</strong> palette{'s' if tot_palettes != 1 else ''}.</p>
                                {pkg_html}
                                <p>Merci,<br>Bon après-midi.</p>
                                <hr>
                                <p><strong>Ferment Station</strong><br>
                                Producteur de boissons fermentées<br>
                                26 Rue Robert Witchitz – 94200 Ivry-sur-Seine</p>
                                """

                            sender_email = os.environ.get("EMAIL_SENDER") or ""
                            recipients = list(to_list)
                            if sender_email and sender_email not in recipients:
                                recipients.append(sender_email)

                            # Envoi unique avec tous les destinataires
                            await asyncio.to_thread(
                                send_html_with_pdf,
                                to_email=recipients,
                                subject=subject,
                                html_body=body,
                                attachments=[(filename, pdf_bytes)],
                            )

                            ui.notify(
                                f"Demande envoyée à {len(to_list)} destinataire(s) !",
                                type="positive", icon="email", position="top",
                            )

                            # ── Sauvegarder dans l'historique ──
                            try:
                                _brassin_id_list = [str(x) for x in (brassin_select.value or [])]
                                lines_payload = [{
                                    "ref": r["ref"],
                                    "produit": r["produit"],
                                    "ddm": r["ddm"],
                                    "cartons": int(r["cartons"]),
                                    "palettes": int(r["palettes"]),
                                    "poids": int(r["poids"]),
                                } for r in active_rows]

                                if is_update:
                                    await asyncio.to_thread(
                                        update_ramasse,
                                        edit_state["editing_id"],
                                        date_ramasse=d,
                                        destinataire=dest_title,
                                        recipients=recipients,
                                        lines=lines_payload,
                                        total_cartons=tot_cartons,
                                        total_palettes=tot_palettes,
                                        total_poids_kg=tot_poids,
                                        packaging=_pkg_lines_email or [],
                                        pdf_bytes=pdf_bytes,
                                        brassin_ids=_brassin_id_list,
                                    )
                                    _log.info(
                                        "Ramasse mise à jour dans l'historique id=%s v%d",
                                        edit_state["editing_id"], next_version,
                                    )
                                    # Sortir du mode édition après un envoi v2+ réussi
                                    _cancel_edit()
                                else:
                                    await asyncio.to_thread(
                                        save_ramasse,
                                        date_ramasse=d,
                                        destinataire=dest_title,
                                        recipients=recipients,
                                        lines=lines_payload,
                                        total_cartons=tot_cartons,
                                        total_palettes=tot_palettes,
                                        total_poids_kg=tot_poids,
                                        packaging=_pkg_lines_email or [],
                                        pdf_bytes=pdf_bytes,
                                        brassin_ids=_brassin_id_list,
                                    )
                                    _log.info("Ramasse sauvegardée dans l'historique")

                                # Refresh la liste de l'historique pour voir la nouvelle entrée
                                _refresh_history()
                            except Exception:
                                _log.warning("Échec sauvegarde historique ramasse", exc_info=True)

                        except (EmailSendError, OSError, ValueError, KeyError) as exc:
                            _log.exception("Erreur envoi email ramasse")
                            ui.notify(f"Erreur envoi : {exc}", type="negative")
                        finally:
                            send_btn_ref.enable()

                    # Dialogue de confirmation avant envoi
                    _email_confirm_dlg, _email_confirm_msg, _email_send_action = confirm_dialog(
                        title="Confirmer l'envoi ?",
                        message="",
                        action_label="Envoyer",
                        action_icon="send",
                    )

                    async def _confirmed_send():
                        _email_confirm_dlg.close()
                        await do_send_email()

                    _email_send_action.on_click(_confirmed_send)
                    send_btn_ref = _email_send_action

                    def _open_email_confirm():
                        emails_raw = email_input.value or ""
                        to_list = [e.strip() for e in emails_raw.split(",") if e.strip()]
                        if not to_list:
                            ui.notify("Indique au moins un destinataire.", type="warning")
                            return
                        _email_confirm_msg.text = (
                            f"L'email sera envoyé à {len(to_list)} destinataire(s) : "
                            f"{', '.join(to_list)}"
                        )
                        _email_confirm_dlg.open()

                    # Label dynamique selon mode (nouveau vs édition)
                    _is_edit_mode = edit_state["editing_id"] is not None
                    _send_label = (
                        f"Envoyer le BL mis à jour (v{int(edit_state['current_version']) + 1})"
                        if _is_edit_mode
                        else "Envoyer la demande"
                    )
                    ui.button(
                        _send_label,
                        icon="send",
                        on_click=_open_email_confirm,
                    ).classes("flex-1").props("color=green-8 unelevated")

            except Exception as exc:  # broad catch: UI error boundary — inner blocks are narrowed
                _log.exception("Erreur construction tableau ramasse")
                with content_container:
                    ui.label(f"Erreur lors de la construction du tableau : {exc}").classes(
                        "text-negative text-body1 q-pa-md"
                    )

        # Watcher sur la sélection
        brassin_select.on_value_change(on_brassins_changed)

        # ── Démarrage du mode édition depuis l'historique ────────────
        async def _start_edit(ramasse_id: str):
            """Charge une ramasse existante et passe en mode édition."""
            rec = await asyncio.to_thread(get_ramasse, ramasse_id)
            if rec is None:
                ui.notify("Ramasse introuvable.", type="negative")
                return
            if rec.get("driver_passed"):
                ui.notify(
                    "Cette ramasse est déjà marquée comme livrée — édition impossible.",
                    type="warning",
                )
                return

            # Restaurer l'état d'édition
            edit_state["editing_id"] = str(rec["id"])
            edit_state["current_version"] = int(rec.get("version") or 1)
            edit_state["previous_lines"] = rec.get("lines") or []
            edit_state["pending_cartons"] = {
                str(line.get("ref")): int(line.get("cartons") or 0)
                for line in (rec.get("lines") or [])
                if line.get("ref")
            }
            # Restaurer les emballages (bouteilles vides, etc.) — matching par label
            edit_state["pending_packaging"] = {
                str(pkg.get("label") or ""): int(pkg.get("qty") or 0)
                for pkg in (rec.get("packaging") or [])
                if pkg.get("label") and int(pkg.get("qty") or 0) > 0
            }

            # Restaurer le destinataire et la date
            dest_name = rec.get("destinataire") or ""
            if dest_name in dest_names:
                dest_select.value = dest_name
            dr = rec.get("date_ramasse")
            if hasattr(dr, "strftime"):
                date_ramasse.value = dr.strftime("%d/%m/%Y")
                try:
                    date_picker.value = dr.isoformat()
                except Exception:
                    pass

            _render_edit_banner()

            # Restaurer la sélection des brassins → déclenche on_brassins_changed
            # qui consommera pending_cartons pour préremplir le tableau.
            brassin_ids = [int(x) for x in (rec.get("brassin_ids") or []) if str(x).isdigit()]
            # Filtrer les brassins qui n'existent plus dans la liste actuelle
            valid_ids = [bid for bid in brassin_ids if bid in brassin_options]
            missing_count = len(brassin_ids) - len(valid_ids)
            brassin_select.value = valid_ids

            if missing_count > 0:
                ui.notify(
                    f"{missing_count} brassin(s) de la ramasse originale ne sont plus disponibles.",
                    type="warning",
                )

            ui.notify(
                f"Mode édition activé — ramasse du {rec.get('date_ramasse')} chargée.",
                type="info", icon="edit_note",
            )

        # ── Section Historique des ramasses ──────────────────────────
        history_container = ui.column().classes("w-full q-mt-lg")

        def _refresh_history():
            """Charge et affiche l'historique des ramasses."""
            history_container.clear()
            with history_container:
                try:
                    total = count_ramasses()
                except Exception:
                    total = 0

                if total == 0:
                    return

                with ui.expansion(
                    f"Historique des ramasses ({total})",
                    icon="history",
                ).classes("w-full").props(
                    "dense header-class='text-subtitle1'"
                ).style(
                    f"border: 1px solid {COLORS.get('border', '#E5E7EB')}; border-radius: 8px"
                ) as hist_exp:

                    hist_data_loaded = {"done": False}

                    def _load_history_data():
                        if hist_data_loaded["done"]:
                            return
                        hist_data_loaded["done"] = True
                        try:
                            items = list_ramasses(limit=20)
                        except Exception:
                            _log.warning("Erreur chargement historique ramasse", exc_info=True)
                            ui.label("Erreur de chargement.").classes("text-negative text-caption")
                            return

                        if not items:
                            ui.label("Aucune ramasse enregistrée.").classes("text-grey-6 q-pa-sm")
                            return

                        hist_cols = [
                            {"name": "date", "label": "Date", "field": "date", "align": "left", "sortable": True},
                            {"name": "dest", "label": "Destinataire", "field": "dest", "align": "left"},
                            {"name": "cartons", "label": "Cartons", "field": "cartons", "align": "right"},
                            {"name": "palettes", "label": "Palettes", "field": "palettes", "align": "right"},
                            {"name": "poids", "label": "Poids (kg)", "field": "poids", "align": "right"},
                            {"name": "statut", "label": "Statut", "field": "statut", "align": "center"},
                            {"name": "actions", "label": "", "field": "actions", "align": "center"},
                        ]

                        hist_rows = []
                        for item in items:
                            dr = item.get("date_ramasse")
                            date_str = dr.strftime("%d/%m/%Y") if hasattr(dr, "strftime") else str(dr)
                            ver = int(item.get("version") or 1)
                            hist_rows.append({
                                "id": str(item["id"]),
                                "date": date_str,
                                "dest": item.get("destinataire", ""),
                                "cartons": item.get("total_cartons", 0),
                                "palettes": item.get("total_palettes", 0),
                                "poids": item.get("total_poids_kg", 0),
                                "version": ver,
                                "driver_passed": bool(item.get("driver_passed", False)),
                                "statut": "",
                                "actions": "",
                            })

                        ht = ui.table(
                            columns=hist_cols,
                            rows=hist_rows,
                            row_key="id",
                            pagination={"rowsPerPage": 10},
                        ).classes("w-full").props("flat bordered dense")

                        # Slot statut : badge version + badge "Livrée"
                        ht.add_slot("body-cell-statut", r'''
                            <q-td :props="props" style="text-align: center">
                                <q-badge v-if="props.row.version > 1" color="orange-8" class="q-mr-xs">
                                    v{{ props.row.version }}
                                </q-badge>
                                <q-badge v-if="props.row.driver_passed" color="green-8">
                                    <q-icon name="local_shipping" size="xs" class="q-mr-xs" />
                                    Livrée
                                </q-badge>
                                <span v-if="props.row.version === 1 && !props.row.driver_passed"
                                      class="text-grey-5 text-caption">—</span>
                            </q-td>
                        ''')

                        # Slot actions : Modifier + Chauffeur passé (si non livrée) + PDF + Renvoyer
                        ht.add_slot("body-cell-actions", r'''
                            <q-td :props="props" style="text-align: center">
                                <q-btn v-if="!props.row.driver_passed"
                                    flat round dense icon="edit" size="sm" color="orange-8"
                                    @click="() => $parent.$emit('edit_hist', {id: props.row.id})" >
                                    <q-tooltip>Modifier (créer une version+1)</q-tooltip>
                                </q-btn>
                                <q-btn v-if="!props.row.driver_passed"
                                    flat round dense icon="local_shipping" size="sm" color="green-7"
                                    @click="() => $parent.$emit('mark_driver_passed', {id: props.row.id})" >
                                    <q-tooltip>Marquer comme livrée (chauffeur passé)</q-tooltip>
                                </q-btn>
                                <q-btn flat round dense icon="picture_as_pdf" size="sm" color="green-8"
                                    @click="() => $parent.$emit('download_hist_pdf', {id: props.row.id})" >
                                    <q-tooltip>Télécharger le PDF</q-tooltip>
                                </q-btn>
                                <q-btn flat round dense icon="forward_to_inbox" size="sm" color="blue-8"
                                    @click="() => $parent.$emit('resend_hist', {id: props.row.id})" >
                                    <q-tooltip>Renvoyer par email</q-tooltip>
                                </q-btn>
                            </q-td>
                        ''')

                        async def _on_download_hist_pdf(e):
                            rid = e.args.get("id")
                            try:
                                rec = await asyncio.to_thread(get_ramasse, rid)
                                if not rec or not rec.get("pdf_bytes"):
                                    ui.notify("PDF non disponible.", type="warning")
                                    return
                                pdf_data = rec["pdf_bytes"]
                                if isinstance(pdf_data, memoryview):
                                    pdf_data = bytes(pdf_data)
                                dr = rec.get("date_ramasse")
                                fname = f"Ramasse_{dr}.pdf" if dr else "Ramasse.pdf"
                                ui.download(pdf_data, fname)
                            except Exception:
                                _log.warning("Erreur téléchargement PDF historique", exc_info=True)
                                ui.notify("Erreur lors du téléchargement.", type="negative")

                        async def _on_resend_hist(e):
                            rid = e.args.get("id")
                            try:
                                rec = await asyncio.to_thread(get_ramasse, rid)
                                if not rec or not rec.get("pdf_bytes"):
                                    ui.notify("PDF non disponible pour renvoi.", type="warning")
                                    return
                                recip = rec.get("recipients") or []
                                if not recip:
                                    ui.notify("Aucun destinataire enregistré.", type="warning")
                                    return
                                pdf_data = rec["pdf_bytes"]
                                if isinstance(pdf_data, memoryview):
                                    pdf_data = bytes(pdf_data)
                                dr = rec.get("date_ramasse")
                                fname = f"Fiche_de_ramasse_{dr}.pdf" if dr else "Fiche_de_ramasse.pdf"
                                subject = f"Demande de ramasse — {dr} — Ferment Station (renvoi)"

                                await asyncio.to_thread(
                                    send_html_with_pdf,
                                    to_email=recip,
                                    subject=subject,
                                    html_body="<p>Bonjour,</p><p>Ci-joint le renvoi de la fiche de ramasse.</p><p>Cordialement,<br>Ferment Station</p>",
                                    attachments=[(fname, pdf_data)],
                                )
                                ui.notify(
                                    f"Ramasse renvoyée à {len(recip)} destinataire(s) !",
                                    type="positive", icon="email",
                                )
                            except Exception:
                                _log.warning("Erreur renvoi email historique", exc_info=True)
                                ui.notify("Erreur lors du renvoi.", type="negative")

                        async def _on_edit_hist(e):
                            rid = e.args.get("id")
                            try:
                                await _start_edit(rid)
                            except Exception:
                                _log.warning("Erreur démarrage édition ramasse", exc_info=True)
                                ui.notify("Erreur lors du chargement de la ramasse.", type="negative")

                        async def _on_mark_driver_passed(e):
                            rid = e.args.get("id")
                            # Confirmation avant verrouillage
                            with ui.dialog() as dlg, ui.card():
                                ui.label("Confirmer : le chauffeur est passé ?").classes("text-h6")
                                ui.label(
                                    "Cette ramasse sera marquée comme livrée et ne pourra plus être modifiée."
                                ).classes("text-body2 text-grey-7")
                                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                                    ui.button("Annuler", on_click=dlg.close).props("flat")
                                    async def _confirm():
                                        dlg.close()
                                        try:
                                            ok = await asyncio.to_thread(mark_driver_passed, rid)
                                            if ok:
                                                ui.notify(
                                                    "Ramasse marquée comme livrée.",
                                                    type="positive", icon="local_shipping",
                                                )
                                                _refresh_history()
                                            else:
                                                ui.notify(
                                                    "Impossible de marquer cette ramasse (déjà livrée ?).",
                                                    type="warning",
                                                )
                                        except Exception:
                                            _log.warning("Erreur marquage driver passed", exc_info=True)
                                            ui.notify("Erreur lors du marquage.", type="negative")
                                    ui.button("Confirmer", icon="local_shipping",
                                              on_click=_confirm).props("color=green-7 unelevated")
                            dlg.open()

                        ht.on("download_hist_pdf", _on_download_hist_pdf)
                        ht.on("resend_hist", _on_resend_hist)
                        ht.on("edit_hist", _on_edit_hist)
                        ht.on("mark_driver_passed", _on_mark_driver_passed)

                    hist_exp.on_value_change(lambda e: _load_history_data() if e.value else None)

        _refresh_history()

        # Rendu initial
        on_brassins_changed()
