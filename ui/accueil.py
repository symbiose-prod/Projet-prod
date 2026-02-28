"""
ui/accueil.py
=============
Page Accueil — Import des données (Easy Beer ou Excel).
"""
from __future__ import annotations

import logging
import os
from io import BytesIO

import pandas as pd
from nicegui import ui, app

_log = logging.getLogger("ferment.accueil")

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
                    "Production. Le fichier contient les volumes vendus "
                    "par produit sur la période choisie — il sert à calculer le plan "
                    "de production optimal et les besoins en emballages."
                ).classes("text-body2").style(f"color: {COLORS['ink2']}; line-height: 1.6")

        # ── Import Easy Beer ─────────────────────────────────────────
        with ui.card().classes("").props("flat bordered").style("width: 50%; min-width: 320px"):
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

                    import_spinner = ui.spinner("dots", size="xl", color="green-8").classes("self-center q-pa-md")
                    import_spinner.set_visibility(False)

                    async def do_import_eb():
                        import asyncio
                        import_btn.disable()
                        import_spinner.set_visibility(True)
                        status_label.set_visibility(False)
                        try:
                            from common.easybeer import get_autonomie_stocks_excel
                            days = int(period_radio.value or 30)
                            xls_bytes = await asyncio.wait_for(
                                asyncio.to_thread(get_autonomie_stocks_excel, days),
                                timeout=45,
                            )
                            df, period = read_input_excel_and_period_from_bytes(xls_bytes)
                            state["imported"] = True
                            state["source"] = "EasyBeer"
                            state["rows"] = len(df)
                            state["window_days"] = days
                            state["df_json"] = df.to_json(orient="split")
                            status_label.text = f"Importé : {len(df)} lignes depuis EasyBeer ({days}j)"
                            status_label.classes("text-positive")
                            status_label.set_visibility(True)
                            ui.notify("Import EasyBeer réussi !", type="positive")
                        except asyncio.TimeoutError:
                            status_label.text = "L'import a dépassé le délai (45 s). Réessayez."
                            status_label.classes("text-negative")
                            status_label.set_visibility(True)
                            ui.notify("Délai dépassé (45 s). Réessayez.", type="warning")
                        except Exception:
                            _log.exception("Erreur import EasyBeer")
                            status_label.text = "Erreur lors de l'import. Vérifiez la connexion EasyBeer."
                            status_label.classes("text-negative")
                            status_label.set_visibility(True)
                        finally:
                            import_spinner.set_visibility(False)
                            import_btn.enable()

                    import_btn = ui.button(
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

            MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 Mo

            def handle_upload(e):
                try:
                    content = e.content.read()
                    if isinstance(content, bytes) and len(content) > MAX_UPLOAD_BYTES:
                        upload_status.text = f"Fichier trop volumineux ({len(content) // (1024*1024)} Mo, max 50 Mo)."
                        upload_status.classes("text-negative")
                        upload_status.set_visibility(True)
                        return
                    # Vérifier magic bytes ZIP (PK\x03\x04) — les .xlsx sont des archives ZIP
                    if isinstance(content, bytes) and not content[:4].startswith(b"PK"):
                        upload_status.text = "Fichier invalide — seuls les fichiers Excel (.xlsx) sont acceptés."
                        upload_status.classes("text-negative")
                        upload_status.set_visibility(True)
                        return
                    buf = BytesIO(content) if isinstance(content, bytes) else content
                    df, period = read_input_excel_and_period_from_bytes(buf)
                    state["imported"] = True
                    state["source"] = e.name
                    state["rows"] = len(df)
                    state["window_days"] = period
                    state["df_json"] = df.to_json(orient="split")
                    upload_status.text = f"Importé : {len(df)} lignes depuis {e.name}"
                    upload_status.classes("text-positive")
                    upload_status.set_visibility(True)
                    ui.notify(f"Fichier {e.name} importé !", type="positive")
                except Exception:
                    _log.exception("Erreur import fichier Excel")
                    upload_status.text = "Erreur lors de l'import. Vérifiez le format du fichier."
                    upload_status.classes("text-negative")
                    upload_status.set_visibility(True)

            ui.upload(
                on_upload=handle_upload,
                label="Glisser ou cliquer pour importer",
                auto_upload=True,
            ).props('accept=".xlsx,.xls" flat bordered').classes("w-full")
