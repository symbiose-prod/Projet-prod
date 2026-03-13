"""
ui/accueil.py
=============
Page Accueil — Import des données (Easy Beer ou Excel).
"""
from __future__ import annotations

import logging
from datetime import date
from io import BytesIO

import pandas as pd
from nicegui import app, ui

_log = logging.getLogger("ferment.accueil")

from common.easybeer import is_configured as eb_configured
from common.session_store import load_df, store_df
from core.optimizer import read_input_excel_and_period_from_bytes
from ui.auth import require_auth
from ui.theme import COLORS, page_layout

# ─── Citations du jour ─────────────────────────────────────────────────────

QUOTES = [
    ("La fermentation est la force vitale de la nature.", "Sandor Ellix Katz"),
    ("Rien de grand ne s'est fait sans passion.", "Hegel"),
    ("La patience est l'art d'espérer.", "Vauvenargues"),
    ("Le microbe n'est rien, le terrain est tout.", "Claude Bernard"),
    ("La simplicité est la sophistication suprême.", "Léonard de Vinci"),
    ("Ce qui ne se mesure pas ne s'améliore pas.", "Peter Drucker"),
    ("La nature ne se presse jamais, et pourtant tout s'accomplit.", "Lao Tseu"),
    ("La qualité n'est jamais un accident, c'est toujours le résultat d'un effort intelligent.", "John Ruskin"),
    ("Chaque grande réalisation commence par une petite action.", "Proverbe"),
    ("Le secret du changement est de concentrer toute son énergie non pas à lutter contre le passé, mais à construire l'avenir.", "Socrate"),
    ("La créativité, c'est l'intelligence qui s'amuse.", "Albert Einstein"),
    ("Bien faire et laisser dire.", "Proverbe français"),
    ("Il n'y a pas de raccourci vers la qualité.", "Proverbe artisan"),
    ("L'essentiel est invisible pour les yeux.", "Saint-Exupéry"),
    ("La vie, c'est comme une bicyclette, il faut avancer pour ne pas perdre l'équilibre.", "Albert Einstein"),
    ("Commencez là où vous êtes. Utilisez ce que vous avez. Faites ce que vous pouvez.", "Arthur Ashe"),
    ("Le temps est le meilleur des ferments.", "Proverbe brasseur"),
    ("On ne fait bien que ce qu'on aime.", "Proverbe"),
    ("La régularité est la mère de la maîtrise.", "Proverbe artisan"),
    ("Un bon produit parle de lui-même.", "Proverbe"),
    ("Chaque jour est une chance de créer quelque chose de beau.", "Proverbe"),
    ("La persévérance n'est pas une longue course, c'est plusieurs petites courses l'une après l'autre.", "Walter Elliot"),
    ("Le travail bien fait porte sa récompense en lui-même.", "Proverbe"),
    ("La meilleure façon de prédire l'avenir est de le créer.", "Peter Drucker"),
    ("Dans chaque difficulté se cache une opportunité.", "Albert Einstein"),
    ("La passion est le sel de la vie.", "Proverbe"),
    ("L'excellence est un art que l'on atteint par l'exercice constant.", "Aristote"),
    ("Qui sème la qualité récolte la confiance.", "Proverbe"),
    ("Rien n'est permanent sauf le changement.", "Héraclite"),
    ("La nature fait bien les choses, aidons-la un peu.", "Proverbe fermenteur"),
    ("Le progrès naît de la diversité des expériences.", "Proverbe"),
]


def _quote_of_the_day() -> tuple[str, str]:
    """Retourne la citation du jour (change chaque jour)."""
    day_index = date.today().timetuple().tm_yday % len(QUOTES)
    return QUOTES[day_index]

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
    df = load_df(raw_json)
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

        # ── Import Easy Beer + Citation ──────────────────────────────
        with ui.row().classes("w-full items-stretch gap-4").style("flex-wrap: wrap"):

            with ui.card().classes("").props("flat bordered").style("flex: 1; min-width: 320px; max-width: 50%"):
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

                        cancel_btn = ui.button(
                            "Annuler", icon="cancel",
                        ).classes("w-full q-mt-xs").props("flat color=grey-7")
                        cancel_btn.set_visibility(False)

                        async def do_import_eb():
                            import asyncio
                            _cancelled = {"v": False}

                            def _do_cancel():
                                _cancelled["v"] = True
                                cancel_btn.set_visibility(False)
                                import_spinner.set_visibility(False)
                                import_btn.enable()
                                status_label.text = "Import annulé."
                                status_label.classes("text-grey-6")
                                status_label.set_visibility(True)

                            cancel_btn.on("click", _do_cancel)
                            import_btn.disable()
                            import_spinner.set_visibility(True)
                            cancel_btn.set_visibility(True)
                            status_label.set_visibility(False)
                            try:
                                from common.easybeer import get_autonomie_stocks_excel
                                days = int(period_radio.value or 30)
                                xls_bytes = await asyncio.wait_for(
                                    asyncio.to_thread(get_autonomie_stocks_excel, days),
                                    timeout=45,
                                )
                                if _cancelled["v"]:
                                    return
                                df, period = read_input_excel_and_period_from_bytes(xls_bytes)
                                state["imported"] = True
                                state["source"] = "EasyBeer"
                                state["rows"] = len(df)
                                state["window_days"] = days
                                state["df_json"] = store_df(df)
                                status_label.text = f"Importé : {len(df)} lignes depuis EasyBeer ({days}j)"
                                status_label.classes("text-positive")
                                status_label.set_visibility(True)
                                ui.notify("Import EasyBeer réussi !", type="positive")
                            except TimeoutError:
                                if _cancelled["v"]:
                                    return
                                status_label.text = "L'import a dépassé le délai (45 s). Réessayez."
                                status_label.classes("text-negative")
                                status_label.set_visibility(True)
                                ui.notify("Délai dépassé (45 s). Réessayez.", type="warning")
                            except Exception:
                                if _cancelled["v"]:
                                    return
                                _log.exception("Erreur import EasyBeer")
                                status_label.text = "Erreur lors de l'import. Vérifiez la connexion EasyBeer."
                                status_label.classes("text-negative")
                                status_label.set_visibility(True)
                            finally:
                                import_spinner.set_visibility(False)
                                cancel_btn.set_visibility(False)
                                import_btn.enable()

                        import_btn = ui.button(
                            "Importer depuis Easy Beer",
                            icon="cloud_download",
                            on_click=do_import_eb,
                        ).classes("w-full q-mt-md").props("color=green-8 unelevated")

            # ── Citation du jour ───────────────────────────────────────
            quote_text, quote_author = _quote_of_the_day()
            with ui.card().classes("flex-1").props("flat bordered").style(
                "min-width: 250px; display: flex; align-items: center; justify-content: center"
            ):
                with ui.card_section().classes("q-pa-lg text-center"):
                    ui.icon("format_quote", size="md").style(
                        f"color: {COLORS['green']}; opacity: 0.3"
                    )
                    ui.label(f"\u00ab {quote_text} \u00bb").classes("text-body1 q-mt-sm").style(
                        f"color: {COLORS['ink']}; font-style: italic; line-height: 1.6"
                    )
                    ui.label(f"\u2014 {quote_author}").classes("text-caption q-mt-md").style(
                        f"color: {COLORS['ink2']}; font-weight: 500"
                    )

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

            # Loading indicator (visible pendant le parsing)
            with ui.row().classes("items-center gap-2") as upload_loading:
                ui.spinner("dots", size="sm", color="green-8")
                ui.label("Analyse du fichier en cours…").classes("text-body2").style(f"color: {COLORS['ink2']}")
            upload_loading.set_visibility(False)

            MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 Mo

            def handle_upload(e):
                upload_loading.set_visibility(True)
                upload_status.set_visibility(False)
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
                    state["df_json"] = store_df(df)
                    upload_status.text = f"Importé : {len(df)} lignes depuis {e.name}"
                    upload_status.classes("text-positive")
                    upload_status.set_visibility(True)
                    ui.notify(f"Fichier {e.name} importé !", type="positive")
                except Exception:
                    _log.exception("Erreur import fichier Excel")
                    upload_status.text = "Erreur lors de l'import. Vérifiez le format du fichier."
                    upload_status.classes("text-negative")
                    upload_status.set_visibility(True)
                finally:
                    upload_loading.set_visibility(False)

            ui.upload(
                on_upload=handle_upload,
                label="Glisser ou cliquer pour importer",
                auto_upload=True,
            ).props('accept=".xlsx,.xls" flat bordered').classes("w-full")
