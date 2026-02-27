"""
ui/ramasse.py
=============
Page Fiche de ramasse — NiceGUI + Quasar Table.

Réutilise toute la logique métier de common/ramasse.py et common/easybeer.py.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import os

from nicegui import ui

_log = logging.getLogger("ferment.ramasse")

from ui.auth import require_auth
from ui.theme import page_layout, kpi_card, section_title, COLORS

from common.ramasse import (
    today_paris,
    clean_product_label,
    parse_barcode_matrix,
    build_ramasse_lines,
    load_destinataires,
    PALETTE_EMPTY_WEIGHT,
)
from common.easybeer import (
    is_configured as eb_configured,
    get_brassins_en_cours,
    get_brassins_archives,
    get_code_barre_matrice,
    get_warehouses,
    fetch_carton_weights,
)
from common.xlsx_fill import build_bl_enlevements_pdf
from common.email import send_html_with_pdf


# ─── Helpers ────────────────────────────────────────────────────────────────

def _load_brassins() -> tuple[list[dict], list[str]]:
    """Charge brassins en cours + 3 derniers archivés. Retourne (brassins, erreurs)."""
    errors: list[str] = []

    try:
        en_cours = get_brassins_en_cours()
    except Exception as exc:
        errors.append(f"Brassins en cours : {exc}")
        en_cours = []

    en_cours_ids = {b.get("idBrassin") for b in en_cours}
    try:
        archives = get_brassins_archives(nombre=3)
        for b in archives:
            if b.get("idBrassin") not in en_cours_ids:
                b["_is_archive"] = True
                en_cours.append(b)
    except Exception as exc:
        errors.append(f"Brassins archivés : {exc}")
    return [b for b in en_cours if not b.get("annule")], errors


def _load_cb_matrix() -> dict[int, list[dict]] | None:
    try:
        raw = get_code_barre_matrice()
        return parse_barcode_matrix(raw)
    except Exception:
        _log.warning("Impossible de charger la matrice codes-barres", exc_info=True)
        return None


def _load_eb_weights() -> dict[tuple[int, str], float] | None:
    try:
        return fetch_carton_weights()
    except Exception:
        _log.warning("Impossible de charger les poids cartons", exc_info=True)
        return None


def _load_entrepot() -> int | None:
    try:
        warehouses = get_warehouses()
        for w in warehouses:
            if w.get("principal"):
                return w.get("idEntrepot")
        return warehouses[0].get("idEntrepot") if warehouses else None
    except Exception:
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

@ui.page("/ramasse")
def page_ramasse():
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

        # ── Chargement données ────────────────────────────────────
        brassins, load_errors = _load_brassins()

        cb_by_product = _load_cb_matrix()
        id_entrepot = _load_entrepot()
        eb_weights = _load_eb_weights()

        destinataires = load_destinataires()
        dest_names = [d["name"] for d in destinataires] if destinataires else ["SOFRIPA"]

        if load_errors:
            for err in load_errors:
                ui.label(f"Erreur API : {err}").classes("text-negative text-caption")

        if not brassins:
            ui.label("Aucun brassin disponible dans EasyBeer.").classes("text-grey-6")
            return

        # ── Sidebar : vide (bouton recharger déplacé à droite) ─────────
        with sidebar:
            pass

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
                "flat round color=grey-7"
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
            except Exception as exc:
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
                grid_row = {
                    "ref": r["Référence"],
                    "produit": label,
                    "ddm": r["DDM"].strftime("%d/%m/%Y") if hasattr(r["DDM"], "strftime") else str(r["DDM"]),
                    "cartons": None,
                    "poids_u": float(meta.get("_poids_carton", 0)),
                    "pal_cap": int(meta.get("_palette_capacity", 0)),
                    "palettes": 0,
                    "poids": 0,
                    "poids_display": "—",
                }
                grid_rows.append(grid_row)

            table_ref["rows"] = grid_rows

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
                                )

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
                                )

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
                                )

                # ── Tableau Quasar ─────────────────────────────────
                section_title("Détail produits", "table_chart")

                ui.label(
                    "Saisis le nombre de cartons — palettes et poids se mettent à jour automatiquement."
                ).classes("text-caption text-grey-6 q-mb-xs")

                table = ui.table(
                    columns=TABLE_COLUMNS,
                    rows=grid_rows,
                    row_key="ref",
                    pagination={"rowsPerPage": 0},
                ).classes("w-full").style(
                    f"color: {COLORS['ink']}"
                )
                table.props("flat bordered dense")
                table_ref["table"] = table

                # Slot body — style Forcer + @change auto-recalcul
                ORANGE = COLORS["orange"]
                table.add_slot("body", r'''
                    <q-tr :props="props"
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
                                <span style="color: ''' + ORANGE + r'''; font-weight: 600">
                                    {{ props.row[col.field] }}
                                </span>
                            </template>
                            <template v-else>
                                {{ props.row[col.field] }}
                            </template>
                        </q-td>
                    </q-tr>
                ''')

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

                    table.rows[:] = table_ref["rows"]
                    table.update()
                    _update_kpis()

                table.on("cartons_changed", on_cartons_changed)

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

                    def do_download_pdf():
                        row_data = table_ref["rows"]
                        active_rows = [r for r in row_data if int(r.get("cartons") or 0) > 0]
                        if not active_rows:
                            ui.notify("Aucun carton renseigné.", type="warning")
                            return
                        try:
                            import pandas as pd
                            d = _get_date_ramasse()
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
                            dest_title = dest_select.value
                            _dest = _get_dest_obj()
                            dest_lines = _dest.get("address_lines", []) if _dest else []

                            pdf_bytes = build_bl_enlevements_pdf(
                                date_creation=today_paris(),
                                date_ramasse=d,
                                destinataire_title=dest_title,
                                destinataire_lines=dest_lines,
                                df_lines=df_export[cols],
                            )
                            ui.download(pdf_bytes, f"Fiche_de_ramasse_{d:%Y-%m-%d}.pdf")
                            ui.notify("PDF généré !", type="positive", icon="check")
                        except Exception as exc:
                            ui.notify(f"Erreur PDF : {exc}", type="negative")

                    ui.button(
                        "Télécharger PDF",
                        icon="picture_as_pdf",
                        on_click=do_download_pdf,
                    ).classes("flex-1").props("outline color=green-8")

                    def do_send_email():
                        emails_raw = email_input.value or ""
                        to_list = [e.strip() for e in emails_raw.split(",") if e.strip()]
                        if not to_list:
                            ui.notify("Indique au moins un destinataire.", type="warning")
                            return

                        row_data = table_ref["rows"]
                        active_rows = [r for r in row_data if int(r.get("cartons") or 0) > 0]
                        if not active_rows:
                            ui.notify("Aucun carton renseigné.", type="warning")
                            return

                        try:
                            import pandas as pd
                            d = _get_date_ramasse()
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
                            dest_title = dest_select.value
                            _dest_email = _get_dest_obj()
                            dest_lines = _dest_email.get("address_lines", []) if _dest_email else []

                            pdf_bytes = build_bl_enlevements_pdf(
                                date_creation=today_paris(),
                                date_ramasse=d,
                                destinataire_title=dest_title,
                                destinataire_lines=dest_lines,
                                df_lines=df_export[cols],
                            )

                            tot_palettes = sum(int(r["palettes"]) for r in active_rows)
                            filename = f"Fiche_de_ramasse_{d:%Y%m%d}.pdf"
                            subject = f"Demande de ramasse — {d:%d/%m/%Y} — Ferment Station"
                            body = f"""
                            <p>Bonjour,</p>
                            <p>Nous aurions besoin d'une ramasse pour le {d:%d/%m/%Y}.<br>
                            Pour <strong>{tot_palettes}</strong> palette{'s' if tot_palettes != 1 else ''}.</p>
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

                            for rcpt in recipients:
                                send_html_with_pdf(
                                    to_email=rcpt,
                                    subject=subject,
                                    html_body=body,
                                    attachments=[(filename, pdf_bytes)],
                                )

                            ui.notify(
                                f"Demande envoyée à {len(to_list)} destinataire(s) !",
                                type="positive", icon="email", position="top",
                            )
                        except Exception as exc:
                            ui.notify(f"Erreur envoi : {exc}", type="negative")

                    # Dialogue de confirmation avant envoi
                    with ui.dialog() as _email_confirm_dlg, ui.card().classes("q-pa-lg"):
                        ui.label("Confirmer l'envoi ?").classes("text-subtitle1").style(
                            f"color: {COLORS['ink']}; font-weight: 600"
                        )
                        _email_confirm_msg = ui.label("").classes("text-body2 text-grey-7 q-mt-xs")

                        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                            ui.button("Annuler", on_click=_email_confirm_dlg.close).props("flat color=grey-7")

                            def _confirmed_send():
                                _email_confirm_dlg.close()
                                do_send_email()

                            ui.button(
                                "Envoyer",
                                icon="send",
                                on_click=_confirmed_send,
                            ).props("color=green-8 unelevated")

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

                    ui.button(
                        "Envoyer la demande",
                        icon="send",
                        on_click=_open_email_confirm,
                    ).classes("flex-1").props("color=green-8 unelevated")

            except Exception as exc:
                with content_container:
                    ui.label(f"Erreur lors de la construction du tableau : {exc}").classes(
                        "text-negative text-body1 q-pa-md"
                    )

        # Watcher sur la sélection
        brassin_select.on_value_change(on_brassins_changed)

        # Rendu initial
        on_brassins_changed()
