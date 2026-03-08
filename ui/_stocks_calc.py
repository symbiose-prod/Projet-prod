"""
ui/_stocks_calc.py
==================
Stock duration computation for bottles (contenants) — no UI, thread-safe.

Called via ``asyncio.to_thread(fetch_and_compute, window_days)`` from the
``/stocks`` page.

Data flow
---------
1. ``GET /stock/bouteilles?idUniteVolume=4``  → current stock & seuil per bottle
2. ``POST /stock/contenant/historique``        → movement history over the period
3. Group history by ``record["stock"]``        → match to consolidation ``libelle``
4. Compute daily consumption and remaining stock days
5. Extract supplier dynamically from history ``fournisseur`` field
6. Fallback to config.yaml patterns for items without supplier in history
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("ferment.stocks")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class StockItem:
    """Computed stock metrics for a single contenant type."""

    label: str            # consolidation libelle (e.g. "Bouteille - 0.33L")
    current_stock: float
    unit: str             # "u"
    seuil_bas: float
    consumption: float    # total consumed over period
    window_days: int
    daily_consumption: float
    stock_days: float | None  # current_stock / daily_consumption, or None
    supplier: str | None = None  # dynamically extracted from history


@dataclass
class StockGroup:
    """A supplier group containing one or more stock items."""

    name: str
    icon: str
    items: list[StockItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_all_contenants(
    consolidation: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract all children from the consolidation tree, excluding TOTAL."""
    children = consolidation.get("consolidationsFilles") or []
    result = []
    for entry in children:
        libelle = entry.get("libelle", "")
        if libelle.upper() == "TOTAL":
            continue
        result.append(entry)
        _log.info(
            "Contenant trouvé '%s' → id=%s, stock=%s, seuilBas=%s",
            libelle,
            entry.get("id"),
            entry.get("quantiteVirtuelle"),
            entry.get("seuilBas"),
        )
    return result


def _analyse_history(
    history: list[dict[str, Any]],
    target_libelles: set[str],
) -> tuple[dict[str, float], dict[str, str]]:
    """Analyse history records: compute consumption AND extract suppliers.

    Returns:
        (consumption_map, supplier_map)
        - consumption_map: ``{stock_name: total_consumption}``
        - supplier_map: ``{stock_name: most_recent_supplier}``
    """
    consumption: dict[str, float] = {lib: 0.0 for lib in target_libelles}
    # Track supplier from most recent entry (positive diff) per stock
    supplier: dict[str, str] = {}
    # Track latest date per stock to pick the most recent supplier
    supplier_date: dict[str, str] = {}

    for record in history:
        stock_name = record.get("stock", "")
        if stock_name not in target_libelles:
            continue

        diff = record.get("difference", 0) or 0

        # Consumption: sum negative diffs
        if diff < 0:
            consumption[stock_name] += abs(diff)

        # Supplier: extract from entry records (positive diff with fournisseur)
        if diff > 0:
            fournisseur = record.get("fournisseur") or ""
            if fournisseur and isinstance(fournisseur, str) and fournisseur.strip():
                record_date = record.get("date", "") or ""
                prev_date = supplier_date.get(stock_name, "")
                if record_date >= prev_date:
                    supplier[stock_name] = fournisseur.strip()
                    supplier_date[stock_name] = record_date

    for lib, sup in supplier.items():
        _log.info("Fournisseur dynamique '%s' → '%s'", lib, sup)

    return consumption, supplier


def _assign_groups(
    items: list[StockItem],
    stocks_config: dict[str, Any],
) -> list[StockGroup]:
    """Assign stock items to supplier groups.

    Priority:
    1. Dynamic supplier from ``item.supplier`` (extracted from history)
    2. Fallback: config.yaml pattern matching on ``item.label``
    3. Final fallback: ungrouped bucket
    """
    ungrouped_label = stocks_config.get("ungrouped_label", "Autres contenants")

    # Config-based fallback patterns
    cfg_groups = stocks_config.get("supplier_groups") or []
    cfg_patterns: list[tuple[str, str, list[str]]] = [
        (g["name"], g.get("icon", "category"), [p.lower() for p in g.get("patterns", [])])
        for g in cfg_groups
    ]

    # Collect items per group name
    group_map: dict[str, StockGroup] = {}

    for item in items:
        group_name: str | None = None
        group_icon = "local_shipping"

        # Priority 1: dynamic supplier from history
        if item.supplier:
            group_name = item.supplier
            # Try to find matching icon from config
            for cfg_name, cfg_icon, _ in cfg_patterns:
                if cfg_name.lower() == group_name.lower():
                    group_icon = cfg_icon
                    group_name = cfg_name  # use config casing
                    break

        # Priority 2: config pattern fallback
        if not group_name:
            label_lower = item.label.lower()
            for cfg_name, cfg_icon, patterns in cfg_patterns:
                if any(p in label_lower for p in patterns):
                    group_name = cfg_name
                    group_icon = cfg_icon
                    break

        # Priority 3: ungrouped
        if not group_name:
            group_name = ungrouped_label
            group_icon = "more_horiz"

        # Add to group
        if group_name not in group_map:
            group_map[group_name] = StockGroup(name=group_name, icon=group_icon)
        group_map[group_name].items.append(item)

    # Sort: configured groups first (in order), then dynamic, then ungrouped last
    cfg_order = {g["name"]: i for i, g in enumerate(cfg_groups)}
    result = sorted(
        group_map.values(),
        key=lambda g: (
            0 if g.name in cfg_order else (2 if g.name == ungrouped_label else 1),
            cfg_order.get(g.name, 999),
            g.name,
        ),
    )
    return result


# ---------------------------------------------------------------------------
# Main entry-point (blocking — run in thread)
# ---------------------------------------------------------------------------

def fetch_and_compute(window_days: int) -> list[StockGroup]:
    """Fetch EasyBeer data and compute stock duration for all contenants.

    1. ``GET /stock/bouteilles``     → find all contenants, get current stock
    2. ``POST /stock/contenant/historique`` → get all movements over period
    3. Match history to contenants by ``stock``/``libelle`` field
    4. Compute daily consumption and remaining stock days
    5. Extract supplier from history ``fournisseur`` field (dynamic)
    6. Group by supplier (dynamic first, config.yaml fallback)
    """
    from common.data import get_stocks_config
    from common.easybeer import get_contenant_historique, get_stock_bouteilles
    from common.easybeer._client import _dates

    # Step 1 — get current stock levels for all contenants
    consolidation = get_stock_bouteilles()
    contenants = _extract_all_contenants(consolidation)

    if not contenants:
        _log.warning("Aucun contenant trouvé dans EasyBeer")
        return []

    # Step 2 — fetch movement history for the period (single API call)
    date_debut, date_fin = _dates(window_days)
    history = get_contenant_historique(
        date_debut=date_debut,
        date_fin=date_fin,
    )

    # Step 3 — compute consumption + extract suppliers from history
    target_libelles = {e.get("libelle", "") for e in contenants}
    consumption_map, supplier_map = _analyse_history(history, target_libelles)

    # Step 4 — build StockItem list
    items: list[StockItem] = []

    for entry in contenants:
        libelle = entry.get("libelle", "")
        current_stock = float(entry.get("quantiteVirtuelle", 0) or 0)
        seuil_bas = float(entry.get("seuilBas", 0) or 0)
        consumption = consumption_map.get(libelle, 0.0)
        daily = consumption / window_days if window_days > 0 else 0.0
        stock_days = (current_stock / daily) if daily > 0 else None

        item = StockItem(
            label=libelle,
            current_stock=current_stock,
            unit="u",
            seuil_bas=seuil_bas,
            consumption=consumption,
            window_days=window_days,
            daily_consumption=daily,
            stock_days=stock_days,
            supplier=supplier_map.get(libelle),
        )
        items.append(item)

        _log.info(
            "Stock '%s': stock=%.0f, conso=%.0f/%dj, daily=%.1f, jours=%s, fournisseur=%s",
            libelle,
            item.current_stock,
            item.consumption,
            window_days,
            item.daily_consumption,
            f"{item.stock_days:.1f}" if item.stock_days else "N/A",
            item.supplier or "?",
        )

    # Step 5 — group by supplier (dynamic + config fallback)
    stocks_config = get_stocks_config()
    return _assign_groups(items, stocks_config)
