"""
ui/accueil.py
=============
Page Accueil — Import des données (Easy Beer ou Excel).
"""
from __future__ import annotations

import os
from io import BytesIO

import pandas as pd
from nicegui import ui, app

from ui.auth import require_auth
from ui.theme import page_layout, section_title, COLORS
from common.easybeer import is_configured as eb_configured
from core.optimizer import read_input_excel_and_period_from_bytes


# ─── State helpers ──────────────────────────────────────────────────────────

def _get_state() -> dict:
    """State partagé via app.storage.user pour les données importées."""
    return app.storage.user.setdefault("accueil", {})


def get_df_raw() -> tuple[pd.DataFrame | None, int]:
    """Désérialise le DataFrame stocké dans app.storage.user par la page Accueil."""
    state = app.storage.user.get("accueil", {})
    raw_json = state.get("df_json")
    if not raw_json:
        return None, 0
    df = pd.read_json(raw_json, orient="split")
    return df, state.get("window_days", 30)


# ─── Page ───────────────────────────────────────────────────────────────────

@ui.page("/accueil")
def page_accueil():
    user = require_auth()
    if not user:
        return

    with page_layout("Accueil", "home", "/accueil") as sidebar:

        with sidebar:
            ui.label("Bienvenue !").classes("text-subtitle2 text-grey-7")
            ui.label(user.get("email", "")).classes("text-caption text-grey-5")

        state = _get_state()

        # ── Explication ────────────────────────────────────────────────
        with ui.card().classes("w-full").props("flat bordered"):
            with ui.card_section().classes("q-pa-md"):
                ui.label(
                    "Chargez vos données de ventes pour alimenter les pages "
                    "Production et Achats. Le fichier contient les volumes vendus "
                    "par produit sur la période choisie — il sert à calculer le plan "
                    "de production optimal et les besoins en emballages."
                ).classes("text-body2").style(f"color: {COLORS['ink2']}; line-height: 1.6")

        # ── Import Easy Beer ─────────────────────────────────────────
        with ui.card().classes("w-full").props("flat bordered"):
            with ui.card_section():
                with ui.row().classes("items-center gap-2"):
                    ui.icon("cloud_download", size="sm").style(f"color: {COLORS['green']}")
                    ui.label("Import Easy Beer").classes("text-h6")

            with ui.card_section():
                if not eb_configured():
                    ui.label("EasyBeer non configuré.").classes("text-grey-6")
                else:
                    # Choix de la période par boutons radio
                    ui.label("Période d'analyse").classes("text-caption").style(
                        f"color: {COLORS['ink2']}; font-weight: 500"
                    )
                    period_radio = ui.radio(
                        {30: "1 mois", 60: "2 mois", 90: "3 mois", 180: "6 mois"},
                        value=30,
                    ).props("inline dense color=green-8")

                    status_label = ui.label("").classes("text-body2 q-mt-sm")
                    status_label.set_visibility(False)

                    def do_import_eb():
                        try:
                            from common.easybeer import get_autonomie_stocks_excel
                            days = int(period_radio.value or 30)
                            xls_bytes = get_autonomie_stocks_excel(days)
                            df, period = read_input_excel_and_period_from_bytes(xls_bytes, days)
                            state["imported"] = True
                            state["source"] = "EasyBeer"
                            state["rows"] = len(df)
                            state["window_days"] = days
                            state["df_json"] = df.to_json(orient="split")
                            status_label.text = f"Importé : {len(df)} lignes depuis EasyBeer ({days}j)"
                            status_label.classes("text-positive")
                            status_label.set_visibility(True)
                            ui.notify("Import EasyBeer réussi !", type="positive")
                        except Exception as exc:
                            status_label.text = f"Erreur : {exc}"
                            status_label.classes("text-negative")
                            status_label.set_visibility(True)

                    ui.button(
                        "Importer depuis Easy Beer",
                        icon="cloud_download",
                        on_click=do_import_eb,
                    ).classes("w-full q-mt-md").props("color=green-8 unelevated")

        # ── Statut actuel ────────────────────────────────────────────
        if state.get("imported"):
            with ui.card().classes("w-full").props("flat bordered"):
                with ui.card_section().classes("row items-center gap-3"):
                    ui.icon("check_circle", size="md").style(f"color: {COLORS['success']}")
                    with ui.column().classes("gap-0"):
                        ui.label("Données chargées").classes("text-h6")
                        ui.label(
                            f"Source : {state.get('source', '?')} — "
                            f"{state.get('rows', 0)} lignes — "
                            f"Fenêtre : {state.get('window_days', '?')} jours"
                        ).classes("text-body2 text-grey-6")

        # ── Upload Excel (solution de secours) ─────────────────────
        with ui.expansion(
            "Import manuel (fichier Excel)",
            icon="upload_file",
        ).classes("w-full q-mt-lg").props("dense header-class=text-grey-7").style(
            f"border: 1px solid {COLORS['border']}; border-radius: 8px"
        ):
            ui.label(
                "Solution de secours : si l'import Easy Beer ne fonctionne pas, "
                "vous pouvez importer manuellement le fichier Excel d'autonomie "
                "des stocks exporté depuis Easy Beer."
            ).classes("text-body2 q-mb-md").style(f"color: {COLORS['ink2']}")

            upload_status = ui.label("").classes("text-body2")
            upload_status.set_visibility(False)

            def handle_upload(e):
                try:
                    content = e.content.read()
                    buf = BytesIO(content) if isinstance(content, bytes) else content
                    df, period = read_input_excel_and_period_from_bytes(buf, 30)
                    state["imported"] = True
                    state["source"] = e.name
                    state["rows"] = len(df)
                    state["window_days"] = period
                    state["df_json"] = df.to_json(orient="split")
                    upload_status.text = f"Importé : {len(df)} lignes depuis {e.name}"
                    upload_status.classes("text-positive")
                    upload_status.set_visibility(True)
                    ui.notify(f"Fichier {e.name} importé !", type="positive")
                except Exception as exc:
                    upload_status.text = f"Erreur : {exc}"
                    upload_status.classes("text-negative")
                    upload_status.set_visibility(True)

            ui.upload(
                on_upload=handle_upload,
                label="Glisser ou cliquer pour importer",
                auto_upload=True,
            ).props('accept=".xlsx,.xls" flat bordered').classes("w-full")
