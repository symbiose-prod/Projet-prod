#!/usr/bin/env python3
"""
Prototype NiceGUI — Fiche de ramasse
=====================================
Démo visuelle pour comparer avec la version Streamlit.
Lance avec :  python3 prototype_nicegui.py
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

from nicegui import ui, app

# ─── Données de démo (pas besoin d'EasyBeer pour le proto) ─────────────────

DEMO_BRASSINS = [
    {"idBrassin": 1, "nom": "KPE20022026", "produit": "Kéfir Pêche", "volume": 1704, "archive": True},
    {"idBrassin": 2, "nom": "KDF16022026", "produit": "Kéfir Original", "volume": 4185, "archive": True},
    {"idBrassin": 3, "nom": "KGI13022026", "produit": "Kéfir Gingembre", "volume": 7200, "archive": True},
    {"idBrassin": 4, "nom": "IPZ18022026", "produit": "Infusion Zest d'agrumes", "volume": 7200, "archive": False},
]

DEMO_ROWS = [
    {"ref": "427014", "produit": "Kéfir Pêche — 12x33cl",       "ddm": "20/02/2027", "cartons": 142, "poids_u": 6.741, "pal_cap": 126},
    {"ref": "427021", "produit": "Kéfir Pêche — 6x75cl",        "ddm": "20/02/2027", "cartons": 0,   "poids_u": 7.23,  "pal_cap": 96},
    {"ref": "427038", "produit": "Kéfir Original — 12x33cl",    "ddm": "16/02/2027", "cartons": 348, "poids_u": 6.741, "pal_cap": 126},
    {"ref": "427045", "produit": "Kéfir Original — 6x75cl",     "ddm": "16/02/2027", "cartons": 56,  "poids_u": 7.23,  "pal_cap": 96},
    {"ref": "427052", "produit": "Kéfir Gingembre — 12x33cl",   "ddm": "13/02/2027", "cartons": 504, "poids_u": 6.741, "pal_cap": 126},
    {"ref": "427069", "produit": "Kéfir Gingembre — 4x75cl",    "ddm": "13/02/2027", "cartons": 0,   "poids_u": 4.68,  "pal_cap": 112},
    {"ref": "427076", "produit": "Infusion Zest d'agrumes — 12x33cl", "ddm": "18/02/2027", "cartons": 600, "poids_u": 6.741, "pal_cap": 126},
]

PALETTE_EMPTY_WEIGHT = 25.0
DEST_OPTIONS = ["SOFRIPA", "STEF", "Autre"]

# ─── Couleurs Symbiose ──────────────────────────────────────────────────────

SYMBIOSE_GREEN = "#2E7D32"
SYMBIOSE_LIGHT = "#E8F5E9"
NIKO_ORANGE = "#F57C00"


def compute_row(row: dict) -> dict:
    """Calcule palettes et poids pour une ligne."""
    cartons = int(row.get("cartons") or 0)
    pal_cap = row.get("pal_cap", 0)
    poids_u = row.get("poids_u", 0)
    nb_pal = math.ceil(cartons / pal_cap) if pal_cap > 0 and cartons > 0 else 0
    poids = round(cartons * poids_u + nb_pal * PALETTE_EMPTY_WEIGHT, 0)
    return {**row, "palettes": nb_pal, "poids": int(poids)}


# ─── Page principale ────────────────────────────────────────────────────────

@ui.page("/")
def page_ramasse():
    # --- Variables réactives ---
    date_ramasse = dt.date.today()
    rows_data = [compute_row(r) for r in DEMO_ROWS]

    # ─── Header ──────────────────────────────────────────────────────────
    with ui.header().classes("items-center justify-between px-6").style(
        f"background: linear-gradient(135deg, {SYMBIOSE_GREEN}, #1B5E20)"
    ):
        with ui.row().classes("items-center gap-4"):
            ui.icon("local_shipping", size="md").classes("text-white")
            ui.label("Ferment Station").classes("text-white text-h5 font-bold")
        with ui.row().classes("items-center gap-2"):
            ui.label("Nicolas").classes("text-white text-body1")
            ui.avatar("N", color=NIKO_ORANGE, text_color="white", size="md")

    # ─── Drawer (sidebar) ────────────────────────────────────────────────
    with ui.left_drawer(value=True, bordered=True).classes("bg-grey-1 q-pa-md") as drawer:
        ui.label("Navigation").classes("text-h6 text-grey-8 q-mb-md")

        nav_items = [
            ("home",          "Accueil",           "/"),
            ("factory",       "Production",        None),
            ("tune",          "Optimisation",      None),
            ("local_shipping","Fiche de ramasse",  "/"),
            ("shopping_cart", "Achats",            None),
        ]
        for icon, label, href in nav_items:
            active = label == "Fiche de ramasse"
            btn = ui.button(
                label,
                icon=icon,
                on_click=lambda: None,
            ).classes("w-full justify-start").props(
                f'flat align=left {"color=green-8" if active else "color=grey-7"}'
            )
            if active:
                btn.style(f"background: {SYMBIOSE_LIGHT} !important; font-weight: 600")

        ui.separator().classes("q-my-lg")
        ui.label("Paramètres").classes("text-subtitle2 text-grey-7 q-mb-sm")

        date_input = ui.date(value=date_ramasse.isoformat()).classes("w-full").props(
            'label="Date de ramasse" outlined dense'
        )
        ui.select(
            DEST_OPTIONS,
            value="SOFRIPA",
            label="Destinataire",
        ).classes("w-full q-mt-sm").props("outlined dense")

    # ─── Contenu principal ───────────────────────────────────────────────
    with ui.column().classes("w-full max-w-7xl mx-auto q-pa-lg gap-6"):

        # Titre
        with ui.row().classes("items-center gap-3 q-mb-sm"):
            ui.icon("local_shipping", size="lg", color=SYMBIOSE_GREEN)
            ui.label("Fiche de ramasse").classes("text-h4 text-grey-9 font-bold")

        # ─── Sélection brassins ──────────────────────────────────────────
        with ui.card().classes("w-full").props("flat bordered"):
            with ui.card_section():
                ui.label("Sélection des brassins").classes("text-h6 text-grey-8")
            with ui.card_section():
                brassin_options = {
                    b["idBrassin"]: f'{b["nom"]} — {b["produit"]} — {b["volume"]:.0f}L'
                    + (" [archivé]" if b["archive"] else "")
                    for b in DEMO_BRASSINS
                }
                ui.select(
                    brassin_options,
                    multiple=True,
                    value=[4, 3],
                    label="Brassins à inclure",
                ).classes("w-full").props('outlined use-chips')

        # ─── KPIs ────────────────────────────────────────────────────────
        active_rows = [r for r in rows_data if r["cartons"] > 0]
        tot_cartons = sum(r["cartons"] for r in active_rows)
        tot_palettes = sum(r["palettes"] for r in active_rows)
        tot_poids = sum(r["poids"] for r in active_rows)

        with ui.row().classes("w-full gap-4"):
            for icon, label, value, color in [
                ("inventory_2",    "Total cartons",    f"{tot_cartons:,}".replace(",", " "),  SYMBIOSE_GREEN),
                ("view_in_ar",     "Total palettes",   str(tot_palettes),                     NIKO_ORANGE),
                ("scale",          "Poids total (kg)", f"{tot_poids:,}".replace(",", " "),    "#1565C0"),
            ]:
                with ui.card().classes("flex-1 q-pa-none").props("flat bordered"):
                    with ui.card_section().classes("row items-center gap-3 q-pa-md"):
                        with ui.element("div").classes("rounded-full q-pa-sm").style(
                            f"background: {color}15"
                        ):
                            ui.icon(icon, size="md").style(f"color: {color}")
                        with ui.column().classes("gap-0"):
                            ui.label(label).classes("text-caption text-grey-6 uppercase")
                            ui.label(value).classes("text-h5 font-bold text-grey-9")

        # ─── Tableau AG Grid ─────────────────────────────────────────────
        with ui.card().classes("w-full").props("flat bordered"):
            with ui.card_section().classes("row items-center justify-between"):
                ui.label("Détail produits").classes("text-h6 text-grey-8")
                ui.badge(f"{len(active_rows)} produit{'s' if len(active_rows) != 1 else ''}", color=SYMBIOSE_GREEN).props("outline")

            grid = ui.aggrid({
                "defaultColDef": {
                    "sortable": True,
                    "resizable": True,
                },
                "columnDefs": [
                    {"field": "ref",       "headerName": "Référence", "width": 110, "pinned": "left"},
                    {"field": "produit",   "headerName": "Produit (goût + format)", "flex": 2, "minWidth": 250},
                    {"field": "ddm",       "headerName": "DDM", "width": 120},
                    {"field": "date_ramasse", "headerName": "Date ramasse", "width": 135,
                     "editable": True, "cellEditor": "agDateStringCellEditor"},
                    {"field": "cartons",   "headerName": "Cartons", "width": 100,
                     "editable": True, "type": "numericColumn",
                     "cellStyle": {"fontWeight": "bold"}},
                    {"field": "palettes",  "headerName": "Palettes", "width": 100,
                     "type": "numericColumn",
                     "cellStyle": {"color": NIKO_ORANGE, "fontWeight": "600"}},
                    {"field": "poids",     "headerName": "Poids (kg)", "width": 110,
                     "type": "numericColumn",
                     "valueFormatter": "value ? value.toLocaleString('fr-FR') + ' kg' : '—'"},
                ],
                "rowData": [
                    {
                        **r,
                        "date_ramasse": dt.date.today().strftime("%d/%m/%Y"),
                    }
                    for r in rows_data
                ],
                "rowClassRules": {
                    "text-grey-4": "data.cartons === 0",
                },
                "animateRows": True,
                "domLayout": "autoHeight",
            }).classes("w-full").style("--ag-header-background-color: #F5F5F5; --ag-odd-row-background-color: #FAFAFA;")

        # ─── Actions ─────────────────────────────────────────────────────
        with ui.card().classes("w-full").props("flat bordered"):
            with ui.card_section():
                ui.label("Export et envoi").classes("text-h6 text-grey-8")
            with ui.card_section():
                with ui.row().classes("w-full gap-3"):
                    ui.button(
                        "Télécharger PDF",
                        icon="picture_as_pdf",
                        on_click=lambda: ui.notify("PDF généré !", type="positive", icon="check"),
                    ).classes("flex-1").props(f'outline color=green-8')

                    ui.button(
                        "Envoyer la demande de ramasse",
                        icon="send",
                        on_click=lambda: ui.notify(
                            "Demande envoyée à SOFRIPA !",
                            type="positive",
                            icon="email",
                            position="top",
                        ),
                    ).classes("flex-1").props(f'color=green-8')

                ui.separator().classes("q-my-sm")
                with ui.row().classes("w-full items-center gap-3"):
                    ui.input(
                        label="Destinataires email",
                        value="transport@sofripa.fr, hello@symbiose-kefir.fr",
                    ).classes("flex-1").props("outlined dense")
                    ui.label("Expéditeur : hello@symbiose-kefir.fr").classes("text-caption text-grey-6")


# ─── Config & lancement ─────────────────────────────────────────────────────

ui.run(
    title="Ferment Station — Prototype NiceGUI",
    port=8502,
    reload=False,
    show=False,
    favicon="🚚",
    dark=False,
)
