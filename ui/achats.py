"""
ui/achats.py
============
Page Achats — Stocks et consommations conditionnements.

Réutilise common/easybeer.py pour les données stock et consommation.
"""
from __future__ import annotations

import asyncio

import pandas as pd
from nicegui import ui, app

from ui.auth import require_auth
from ui.theme import page_layout, kpi_card, section_title, COLORS
from common.easybeer import is_configured as eb_configured


@ui.page("/achats")
def page_achats():
    user = require_auth()
    if not user:
        return

    with page_layout("Achats conditionnements", "shopping_cart", "/achats") as sidebar:

        # ── Sidebar : paramètres ─────────────────────────────────────
        with sidebar:
            ui.label("Paramètres").classes("text-subtitle2 text-grey-7")

            window_input = ui.number(
                "Fenêtre conso (jours)", value=30, min=7, max=365,
            ).props("outlined dense")

            horizon_input = ui.number(
                "Horizon commande (jours)", value=30, min=1, max=365,
            ).props("outlined dense")

            ui.separator().classes("q-my-sm")
            ui.label("Seuils d'alerte").classes("text-subtitle2 text-grey-7")

            seuil_rouge = ui.number(
                "Critique < (jours)", value=7, min=1, max=90,
            ).props("outlined dense")

            seuil_orange = ui.number(
                "Attention < (jours)", value=21, min=1, max=180,
            ).props("outlined dense")

            ui.separator().classes("q-my-sm")
            ui.label("Filtres").classes("text-subtitle2 text-grey-7")

            include_contenants = ui.checkbox("Inclure bouteilles vides", value=True)
            masquer_sans_conso = ui.checkbox("Masquer sans consommation", value=False)

        # ── Guards ───────────────────────────────────────────────────
        if not eb_configured():
            ui.label("EasyBeer non configuré.").classes("text-negative q-pa-md")
            return

        # ── Bouton sync ──────────────────────────────────────────────
        sync_status = ui.label("").classes("text-body2")
        sync_status.set_visibility(False)

        # Containers pour les sections
        produits_finis_container = ui.column().classes("w-full gap-4")
        composants_container = ui.column().classes("w-full gap-4")
        commande_container = ui.column().classes("w-full gap-4")

        # Spinner de chargement (masqué par défaut)
        sync_spinner = ui.spinner("dots", size="xl", color="green-8").classes("self-center q-pa-md")
        sync_spinner.set_visibility(False)

        async def do_sync():
            # Feedback visuel : spinner + désactiver le bouton
            sync_btn.disable()
            sync_spinner.set_visibility(True)
            sync_status.set_visibility(False)
            try:
                from common.easybeer import (
                    get_autonomie_stocks,
                    get_mp_all,
                    get_synthese_consommations_mp,
                )
                try:
                    days = max(1, int(window_input.value or 30))
                except (ValueError, TypeError):
                    days = 30
                try:
                    horizon = max(1, int(horizon_input.value or 30))
                except (ValueError, TypeError):
                    horizon = 30

                # 1-2-3. Appels API en parallèle (timeout 45s > HTTP timeout 30s)
                _API_TIMEOUT = 45
                autonomie, mp_all, conso = await asyncio.wait_for(
                    asyncio.gather(
                        asyncio.to_thread(get_autonomie_stocks, days),
                        asyncio.to_thread(get_mp_all),
                        asyncio.to_thread(get_synthese_consommations_mp, days),
                    ),
                    timeout=_API_TIMEOUT,
                )
                produits = autonomie.get("produits", [])

                try:
                    seuil_r = max(1, int(seuil_rouge.value or 7))
                except (ValueError, TypeError):
                    seuil_r = 7
                try:
                    seuil_o = max(1, int(seuil_orange.value or 21))
                except (ValueError, TypeError):
                    seuil_o = 21

                # ── Section 1 : Produits finis ───────────────────────
                produits_finis_container.clear()
                with produits_finis_container:
                    section_title("Durée de stock — Produits finis", "inventory")

                    n_rouge = sum(1 for p in produits if (p.get("autonomie") or 0) < seuil_r)
                    n_orange = sum(1 for p in produits if seuil_r <= (p.get("autonomie") or 0) < seuil_o)
                    n_vert = sum(1 for p in produits if (p.get("autonomie") or 0) >= seuil_o)

                    with ui.row().classes("w-full gap-4"):
                        kpi_card("error", f"Critique (<{seuil_r}j)", str(n_rouge), COLORS["error"])
                        kpi_card("warning", f"Attention (<{seuil_o}j)", str(n_orange), COLORS["orange"])
                        kpi_card("check_circle", "OK", str(n_vert), COLORS["success"])

                    pf_rows = []
                    for p in produits:
                        auto = p.get("autonomie") or 0
                        status = "🔴" if auto < seuil_r else ("🟡" if auto < seuil_o else "🟢")
                        pf_rows.append({
                            "produit": p.get("libelle", "?"),
                            "stock": int(p.get("quantiteVirtuelle") or 0),
                            "volume": round(p.get("volume") or 0, 1),
                            "duree": round(auto, 1),
                            "status": status,
                        })
                    pf_rows.sort(key=lambda r: r["duree"])

                    ui.aggrid({
                        "columnDefs": [
                            {"field": "status", "headerName": "", "width": 50, "sortable": False},
                            {"field": "produit", "headerName": "Produit", "flex": 2},
                            {"field": "stock", "headerName": "Stock (u)", "width": 100, "type": "numericColumn"},
                            {"field": "volume", "headerName": "Volume (hL)", "width": 110, "type": "numericColumn"},
                            {"field": "duree", "headerName": "Durée (j)", "width": 100, "type": "numericColumn"},
                        ],
                        "rowData": pf_rows,
                        "domLayout": "autoHeight",
                    }).classes("w-full")

                # ── Section 2 : Composants ───────────────────────────
                composants_container.clear()
                with composants_container:
                    section_title("Durée de stock — Composants emballage", "widgets")

                    # Filtrer composants
                    condo_types = {"CONDITIONNEMENT"}
                    if include_contenants.value:
                        condo_types.add("CONTENANT")

                    composants = [
                        m for m in mp_all
                        if (m.get("type") or {}).get("code") in condo_types
                    ]

                    # Index consommation
                    conso_elements = {}
                    section_keys = ["syntheseConditionnement"]
                    if include_contenants.value:
                        section_keys.append("syntheseContenant")
                    for section_key in section_keys:
                        elts = (conso.get(section_key) or {}).get("elements", [])
                        for e in elts:
                            conso_elements[e.get("idMatierePremiere")] = float(e.get("quantite") or 0)

                    comp_rows = []
                    for m in composants:
                        mid = m.get("idMatierePremiere")
                        stock = float(m.get("quantiteVirtuelle") or 0)
                        qty_conso = conso_elements.get(mid, 0)
                        conso_jour = qty_conso / days if days > 0 else 0
                        duree = stock / conso_jour if conso_jour > 0 else 999

                        if masquer_sans_conso.value and qty_conso == 0:
                            continue

                        status = "🔴" if duree < seuil_r else ("🟡" if duree < seuil_o else "🟢")
                        comp_rows.append({
                            "status": status,
                            "composant": m.get("libelle", "?"),
                            "type": (m.get("type") or {}).get("code", "?"),
                            "stock": round(stock, 0),
                            "unite": (m.get("unite") or {}).get("symbole", "?"),
                            "conso": round(qty_conso, 0),
                            "conso_jour": round(conso_jour, 1),
                            "duree": round(duree, 1) if duree < 999 else "∞",
                            # Données pour la commande recommandée
                            "_stock_num": stock,
                            "_conso_jour_num": conso_jour,
                        })
                    comp_rows.sort(key=lambda r: r["duree"] if isinstance(r["duree"], (int, float)) else 9999)

                    # KPIs composants
                    n_rouge_c = sum(1 for r in comp_rows if r["status"] == "🔴")
                    n_orange_c = sum(1 for r in comp_rows if r["status"] == "🟡")
                    n_vert_c = sum(1 for r in comp_rows if r["status"] == "🟢")

                    with ui.row().classes("w-full gap-4"):
                        kpi_card("error", f"Critique (<{seuil_r}j)", str(n_rouge_c), COLORS["error"])
                        kpi_card("warning", f"Attention (<{seuil_o}j)", str(n_orange_c), COLORS["orange"])
                        kpi_card("check_circle", "OK", str(n_vert_c), COLORS["success"])

                    ui.aggrid({
                        "columnDefs": [
                            {"field": "status", "headerName": "", "width": 50},
                            {"field": "composant", "headerName": "Composant", "flex": 2},
                            {"field": "stock", "headerName": "Stock", "width": 90, "type": "numericColumn"},
                            {"field": "unite", "headerName": "Unité", "width": 70},
                            {"field": "conso_jour", "headerName": "Conso/jour", "width": 100, "type": "numericColumn"},
                            {"field": "duree", "headerName": "Durée (j)", "width": 100},
                        ],
                        "rowData": comp_rows,
                        "domLayout": "autoHeight",
                    }).classes("w-full")

                # ── Section 3 : Commande recommandée ─────────────────
                commande_container.clear()
                with commande_container:
                    section_title("Commande recommandée", "shopping_cart")

                    order_rows = []
                    for r in comp_rows:
                        conso_j = r["_conso_jour_num"]
                        stock_n = r["_stock_num"]
                        besoin = conso_j * horizon
                        a_commander = max(0, besoin - stock_n)
                        if a_commander > 0:
                            order_rows.append({
                                "status": r["status"],
                                "composant": r["composant"],
                                "unite": r["unite"],
                                "besoin": round(besoin, 0),
                                "stock": round(stock_n, 0),
                                "a_commander": round(a_commander, 0),
                            })
                    order_rows.sort(key=lambda r: r["a_commander"], reverse=True)

                    n_to_order = len(order_rows)
                    total_units = sum(r["a_commander"] for r in order_rows)

                    with ui.row().classes("w-full gap-4"):
                        kpi_card("shopping_cart", "Composants à commander", str(n_to_order), COLORS["orange"])
                        kpi_card("inventory_2", "Total unités", f"{total_units:,.0f}".replace(",", " "), COLORS["green"])

                    if order_rows:
                        order_grid = ui.aggrid({
                            "columnDefs": [
                                {"field": "status", "headerName": "", "width": 50},
                                {"field": "composant", "headerName": "Composant", "flex": 2},
                                {"field": "unite", "headerName": "Unité", "width": 70},
                                {"field": "besoin", "headerName": f"Besoin ({horizon}j)", "width": 120, "type": "numericColumn"},
                                {"field": "stock", "headerName": "Stock actuel", "width": 110, "type": "numericColumn"},
                                {"field": "a_commander", "headerName": "À commander", "width": 120, "type": "numericColumn",
                                 "cellStyle": {"fontWeight": "bold", "color": COLORS["orange"]}},
                            ],
                            "rowData": order_rows,
                            "domLayout": "autoHeight",
                        }).classes("w-full")

                        async def do_export_csv():
                            rows_data = await order_grid.get_client_data()
                            df = pd.DataFrame(rows_data)
                            cols_export = ["composant", "unite", "besoin", "stock", "a_commander"]
                            cols_export = [c for c in cols_export if c in df.columns]
                            csv_bytes = df[cols_export].to_csv(index=False).encode("utf-8")
                            from common.ramasse import today_paris
                            fname = f"commande_emballages_{horizon}j_{today_paris().isoformat()}.csv"
                            ui.download(csv_bytes, fname)
                            ui.notify("CSV exporté !", type="positive")

                        ui.button(
                            "Exporter la commande (CSV)",
                            icon="download",
                            on_click=do_export_csv,
                        ).classes("w-full q-mt-sm").props("outline color=green-8")
                    else:
                        ui.label("Tous les stocks couvrent l'horizon demandé.").classes("text-grey-6")

                sync_status.text = f"Synchronisé — {len(produits)} produits, {len(composants)} composants"
                sync_status.classes("text-positive")
                sync_status.set_visibility(True)
                ui.notify("Données synchronisées !", type="positive")

            except asyncio.TimeoutError:
                sync_status.text = "La synchronisation a dépassé le délai (45 s). Réessayez."
                sync_status.classes("text-negative")
                sync_status.set_visibility(True)
                ui.notify("Délai dépassé (45 s). Réessayez.", type="warning")
            except Exception as exc:
                sync_status.text = f"Erreur : {exc}"
                sync_status.classes("text-negative")
                sync_status.set_visibility(True)
                ui.notify(f"Erreur sync : {exc}", type="negative")
            finally:
                sync_spinner.set_visibility(False)
                sync_btn.enable()

        sync_btn = ui.button(
            "Synchroniser Easy Beer",
            icon="sync",
            on_click=do_sync,
        ).classes("w-full q-mb-md").props("color=green-8 unelevated")
