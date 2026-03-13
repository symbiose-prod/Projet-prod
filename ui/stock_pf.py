"""
ui/stock_pf.py
==============
Page Stock produits finis — Comparaison EasyBeer vs Sofripa.

Upload du CSV Sofripa (ARTICLES.csv), puis affichage d'un tableau
comparatif avec coloration des écarts.
"""
from __future__ import annotations

import asyncio
import logging

from nicegui import ui

_log = logging.getLogger("ferment.stock_pf")

from common.easybeer import is_configured as eb_configured
from ui._stock_pf_calc import fetch_stock_comparison
from ui.auth import require_auth
from ui.theme import COLORS, kpi_card, page_layout, section_title


def _format_number(n: int) -> str:
    """Formate un nombre avec séparateur de milliers (espace fine insécable)."""
    return f"{n:,}".replace(",", "\u202f")


@ui.page("/stock-pf")
def page_stock_pf():
    require_auth()

    with page_layout("Stock produits finis", "compare_arrows", "/stock-pf"):

        # ── Vérification EasyBeer configuré ──
        if not eb_configured():
            ui.label("EasyBeer n'est pas configuré.").classes("text-body1 text-grey-7")
            return

        # ── État ──
        state = {
            "data": None,
            "show_only_ecarts": False,
        }

        # ── Zone d'upload ──
        section_title("Import du fichier Sofripa", "upload_file")

        # ── Conteneur résultats ──
        results_container = ui.column().classes("w-full gap-4")

        # ── KPIs ──
        kpi_row = ui.row().classes("w-full gap-4 q-mt-md")
        kpi_row.set_visibility(False)

        # ── Tableau ──
        table_container = ui.column().classes("w-full")
        table_container.set_visibility(False)

        async def handle_upload(e):
            """Traite l'upload du CSV et lance la comparaison."""
            content_bytes = e.file.read()
            # Essayer UTF-8, fallback latin-1
            try:
                csv_text = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                csv_text = content_bytes.decode("latin-1")

            # Spinner pendant le chargement
            kpi_row.set_visibility(False)
            table_container.set_visibility(False)
            table_container.clear()
            kpi_row.clear()

            with results_container:
                spinner_row = ui.row().classes("w-full justify-center q-pa-lg")
                with spinner_row:
                    ui.spinner("dots", size="lg", color="green-8")
                    ui.label("Comparaison en cours…").classes("text-body2 text-grey-7")

            try:
                data = await asyncio.to_thread(fetch_stock_comparison, csv_text)
                state["data"] = data
            except Exception as exc:
                _log.error("Erreur comparaison stock: %s", exc, exc_info=True)
                spinner_row.delete()
                with results_container:
                    ui.notification(
                        f"Erreur : {exc}",
                        type="negative",
                        timeout=10000,
                    )
                return

            spinner_row.delete()
            _render_results(data, state, kpi_row, table_container)

        upload_card = ui.card().classes("w-full").props("flat bordered")
        with upload_card:
            with ui.card_section():
                ui.label(
                    "Uploadez le fichier ARTICLES.csv exporté depuis l'interface Sofripa."
                ).classes("text-body2 text-grey-7 q-mb-sm")

                ui.upload(
                    on_upload=handle_upload,
                    label="ARTICLES.csv",
                    auto_upload=True,
                    max_files=1,
                ).props('accept=".csv" flat bordered').classes("w-full")


def _render_results(data: dict, state: dict, kpi_row, table_container):
    """Affiche les KPIs et le tableau comparatif."""
    summary = data["summary"]
    rows = data["rows"]

    # ── KPIs ──
    kpi_row.clear()
    with kpi_row:
        kpi_card(
            "inventory_2",
            "Stock EasyBeer",
            _format_number(summary["total_eb_reel"]),
            color=COLORS["blue"],
        )
        kpi_card(
            "warehouse",
            "Stock Sofripa",
            _format_number(summary["total_sofripa"]),
            color=COLORS["green"],
        )

        ecart_total = summary["total_ecart"]
        ecart_color = COLORS["success"] if ecart_total == 0 else COLORS["error"]
        kpi_card(
            "compare_arrows",
            "Écart total",
            _format_number(ecart_total),
            color=ecart_color,
        )
        kpi_card(
            "warning",
            "Produits en écart",
            f"{summary['nb_ecarts']} / {summary['nb_produits']}",
            color=COLORS["warning"] if summary["nb_ecarts"] > 0 else COLORS["success"],
        )
    kpi_row.set_visibility(True)

    # ── Toggle écarts ──
    table_container.clear()
    with table_container:
        with ui.row().classes("w-full items-center justify-between q-mb-sm"):
            section_title("Détail par produit", "table_chart")
            toggle = ui.switch("Écarts uniquement", value=state["show_only_ecarts"]).props(
                "color=green-8 dense"
            )

        # ── Tableau ──
        columns = [
            {"name": "ref", "label": "Réf.", "field": "ref", "align": "center", "sortable": True, "style": "width: 80px"},
            {"name": "designation", "label": "Désignation", "field": "designation", "align": "left", "sortable": True},
            {"name": "stock_eb_reel", "label": "Stock EB", "field": "stock_eb_reel", "align": "center", "sortable": True},
            {"name": "en_cours_eb", "label": "En cours", "field": "en_cours_eb", "align": "center", "sortable": True},
            {"name": "stock_sofripa", "label": "Stock Sofripa", "field": "stock_sofripa", "align": "center", "sortable": True},
            {"name": "en_prepa", "label": "En prépa", "field": "en_prepa", "align": "center", "sortable": True},
            {"name": "en_recept", "label": "En récept.", "field": "en_recept", "align": "center", "sortable": True},
            {"name": "ecart", "label": "Écart", "field": "ecart", "align": "center", "sortable": True},
        ]

        visible_rows = _filter_rows(rows, state["show_only_ecarts"])

        table = ui.table(
            columns=columns,
            rows=visible_rows,
            row_key="ref",
        ).classes("w-full").props("flat bordered dense separator=cell")

        # Slot body pour coloration conditionnelle de la colonne écart
        table.add_slot("body", r"""
            <q-tr :props="props">
                <q-td v-for="col in props.cols" :key="col.name" :props="props"
                    :style="col.name === 'ecart' ? (
                        props.row.ecart === 0 ? 'color: #16A34A; font-weight: 600' :
                        props.row.ecart > 0 ? 'color: #F59E0B; font-weight: 600' :
                        'color: #EF4444; font-weight: 600'
                    ) : (
                        !props.row.match && col.name === 'designation' ? 'color: #9CA3AF; font-style: italic' : ''
                    )">
                    {{ col.value }}
                </q-td>
            </q-tr>
        """)

        def on_toggle(e):
            state["show_only_ecarts"] = e.value
            table.rows = _filter_rows(rows, e.value)
            table.update()

        toggle.on_value_change(on_toggle)

        # ── Produits non matchés ──
        unmatched_csv = data.get("unmatched_csv", [])
        unmatched_eb = data.get("unmatched_eb", [])

        if unmatched_csv or unmatched_eb:
            ui.separator().classes("q-my-md")
            with ui.expansion("Produits non appariés", icon="info_outline").classes("w-full").props(
                "dense header-class=text-grey-7"
            ):
                if unmatched_csv:
                    ui.label(
                        f"Dans le CSV Sofripa mais pas dans EasyBeer ({len(unmatched_csv)}) :"
                    ).classes("text-body2 text-grey-7 q-mb-xs")
                    for ref in unmatched_csv:
                        csv_row = next((r for r in rows if r["ref"] == ref), None)
                        designation = csv_row["designation"] if csv_row else ref
                        ui.label(f"• {ref} — {designation}").classes("text-body2 q-ml-md")

                if unmatched_eb:
                    ui.label(
                        f"Dans EasyBeer mais pas dans le CSV Sofripa ({len(unmatched_eb)}) :"
                    ).classes("text-body2 text-grey-7 q-mb-xs q-mt-sm")
                    for ref in unmatched_eb:
                        eb_row = next((r for r in rows if r["ref"] == ref), None)
                        designation = eb_row["designation"] if eb_row else ref
                        ui.label(f"• {ref} — {designation}").classes("text-body2 q-ml-md")

    table_container.set_visibility(True)


def _filter_rows(rows: list[dict], ecarts_only: bool) -> list[dict]:
    """Filtre les lignes selon le toggle écarts."""
    if ecarts_only:
        return [r for r in rows if r["ecart"] != 0]
    return rows
