"""
pages/admin_eb_stock_templates.py
==================================
Dashboard admin ``/admin/eb-stock-templates``.

Affiche le contenu de la table ``eb_stock_product_templates`` (templates
"stock produit fini" EasyBeer indexés par codeArticle) et propose un
bouton **Resync from EB** pour repeupler la table via l'API EB.

Cette table est consommée par :

- ``common/services/bottle_stock_resolver.py`` (résolution
  ``(produit, format, marque) → idStockBouteille``)
- ``common/services/mise_en_bouteille_orchestrator.py`` (construction
  du payload ``POST /brassin/mise-en-bouteille``)

Cf. ``docs/easybeer-write-payloads/`` pour le contexte business.
"""
from __future__ import annotations

import json
import logging

from nicegui import ui

from common.easybeer.stock_templates_sync import (
    find_template_by_code_article,
    list_synced_templates,
    sync_all_templates,
)
from pages._admin_helpers import require_admin
from pages.theme import page_layout, section_title

_log = logging.getLogger("ferment.admin_eb_stock_templates")


def _show_template_dialog(tenant_id: str, code_article: str) -> None:
    """Modal affichant le détail complet d'un template (raw_data inclus)."""
    detail = find_template_by_code_article(
        tenant_id=tenant_id, code_article=code_article,
    )
    if not detail:
        ui.notify(f"Template {code_article} introuvable", type="negative")
        return

    with ui.dialog() as dialog, ui.card().classes("w-[900px] max-w-full"):
        ui.label(f"Template {code_article}").classes("text-lg font-bold")
        ui.label(
            f"idStockProduit : {detail.get('id_stock_produit')} | "
            f"idProduit EB : {detail.get('id_produit')}",
        ).classes("text-sm text-gray-600")

        ui.label("Conditionnement :").classes("text-sm font-semibold mt-2")
        ui.label(
            f"{detail.get('contenant_libelle')} — "
            f"{detail.get('lot_libelle')} (PCB={detail.get('lot_quantite')})",
        ).classes("text-sm")

        ui.label("Elements conditionnement (BOM par carton) :").classes(
            "text-sm font-semibold mt-2",
        )
        elements = detail.get("elements_conditionnement") or []
        if isinstance(elements, str):
            try:
                elements = json.loads(elements)
            except ValueError:
                elements = []
        if not elements:
            ui.label("(aucun élément)").classes("text-xs text-gray-500")
        else:
            for el in elements:
                ui.label(
                    f"  • {el.get('libelle', '?')} (idMP {el.get('idMatierePremiere')}) "
                    f"— qté {el.get('quantite')} ({el.get('type', '?')})",
                ).classes("text-xs font-mono")

        if raw := detail.get("raw_data"):
            with ui.expansion("Raw JSON (GET /stock/produit/edition)", icon="code"):
                raw_text = (
                    raw if isinstance(raw, str)
                    else json.dumps(raw, indent=2, ensure_ascii=False, default=str)
                )
                ui.code(raw_text[:5000], language="json").classes(
                    "max-h-96 overflow-auto",
                )

        ui.button("Fermer", on_click=dialog.close).props("flat")
    dialog.open()


def _render_templates_table(
    tenant_id: str,
    templates: list[dict],
) -> None:
    """Rend un tableau lecture seule des templates synced."""
    if not templates:
        ui.label(
            "Aucun template synchronisé. Cliquez sur 'Resync from EB' "
            "pour peupler la table.",
        ).classes("text-sm text-gray-600 italic p-4")
        return

    columns = [
        {"name": "code_article", "label": "Code Article", "field": "code_article",
         "align": "left", "sortable": True},
        {"name": "produit_libelle", "label": "Produit", "field": "produit_libelle",
         "align": "left", "sortable": True},
        {"name": "contenant_libelle", "label": "Contenant",
         "field": "contenant_libelle", "align": "left"},
        {"name": "contenance", "label": "Contenance (L)", "field": "contenance"},
        {"name": "lot_libelle", "label": "Conditionnement", "field": "lot_libelle",
         "align": "left"},
        {"name": "lot_quantite", "label": "PCB", "field": "lot_quantite"},
        {"name": "n_elements", "label": "Éléments BOM", "field": "n_elements"},
        {"name": "synced_at", "label": "Sync", "field": "synced_at"},
    ]
    # Format des dates et noms pour l'affichage
    rows = [
        {
            **t,
            "synced_at": (
                t.get("synced_at").strftime("%Y-%m-%d %H:%M")
                if t.get("synced_at") else ""
            ),
        }
        for t in templates
    ]

    table = ui.table(columns=columns, rows=rows, row_key="code_article").classes(
        "w-full",
    )
    table.add_slot(
        "body-cell-code_article",
        r"""
        <q-td :props="props">
            <q-btn dense flat color="primary" :label="props.value"
                   @click="$parent.$emit('show_detail', props.row)" />
        </q-td>
        """,
    )
    table.on(
        "show_detail",
        lambda e: _show_template_dialog(tenant_id, e.args["code_article"]),
    )


def _trigger_sync(tenant_id: str, refresh_fn) -> None:
    """Lance la sync (sync synchrone — UI bloque temporairement)."""
    ui.notify("Sync en cours… (cela peut prendre 30s à 2min)", type="info")
    try:
        stats = sync_all_templates(tenant_id=tenant_id)
        ui.notify(
            f"Sync terminée : {stats['upserted']} upserted, "
            f"{stats['skipped']} skipped, {stats['errors']} erreurs "
            f"(total {stats['total']})",
            type="positive",
        )
        refresh_fn()
    except Exception as exc:  # noqa: BLE001
        _log.exception("Sync templates échouée")
        ui.notify(f"Erreur sync : {exc}", type="negative", multi_line=True)


@ui.page("/admin/eb-stock-templates")
def page_admin_eb_stock_templates() -> None:
    """Page admin : templates "stock produit" EB (lecture + bouton resync)."""
    user = require_admin()
    if not user:
        return

    tenant_id = user["tenant_id"]
    table_holder = ui.column().classes("w-full")

    def refresh() -> None:
        templates = list_synced_templates(tenant_id=tenant_id)
        table_holder.clear()
        with table_holder:
            ui.label(f"{len(templates)} templates synchronisés").classes(
                "text-sm text-gray-600 mb-2",
            )
            _render_templates_table(tenant_id, templates)

    with page_layout(
        "Templates stocks produits EasyBeer",
        "inventory_2",
        "/admin/eb-stock-templates",
    ):
        section_title("Codes articles EasyBeer — source pour mise-en-bouteille", "code")

        ui.label(
            "Cette table est peuplée par appels API EB (GET /stock/produit/edition/{id}). "
            "Elle sert à résoudre, pour chaque (produit, format, marque) de fiche de production, "
            "le idStockBouteille à débiter lors du POST mise-en-bouteille.",
        ).classes("text-sm text-gray-700 mb-4")

        with ui.row().classes("gap-2 mb-4"):
            ui.button(
                "Resync from EB",
                icon="cloud_sync",
                on_click=lambda: _trigger_sync(tenant_id, refresh),
            ).props("color=primary")
            ui.button(
                "Rafraîchir",
                icon="refresh",
                on_click=refresh,
            ).props("outline")

        refresh()
