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
5. Group items by supplier via config.yaml patterns
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


def _compute_consumption_from_history(
    history: list[dict[str, Any]],
    target_libelles: set[str],
) -> dict[str, float]:
    """Group history records by ``stock`` field and sum negative differences.

    Returns ``{stock_name: total_consumption}``.
    """
    consumption: dict[str, float] = {lib: 0.0 for lib in target_libelles}

    for record in history:
        stock_name = record.get("stock", "")
        if stock_name not in target_libelles:
            continue
        diff = record.get("difference", 0) or 0
        if diff < 0:
            consumption[stock_name] += abs(diff)

    return consumption


def _assign_groups(
    items: list[StockItem],
    stocks_config: dict[str, Any],
) -> list[StockGroup]:
    """Assign stock items to supplier groups based on pattern matching.

    Each item matches the *first* group whose pattern is found (case-insensitive)
    in ``item.label``. Unmatched items go into the fallback group.
    """
    supplier_groups = stocks_config.get("supplier_groups") or []
    ungrouped_label = stocks_config.get("ungrouped_label", "Autres contenants")

    # Build ordered group list
    groups: list[StockGroup] = [
        StockGroup(name=g["name"], icon=g.get("icon", "category"))
        for g in supplier_groups
    ]
    fallback = StockGroup(name=ungrouped_label, icon="more_horiz")

    # Precompute lowered patterns per group
    group_patterns: list[list[str]] = [
        [p.lower() for p in g.get("patterns", [])]
        for g in supplier_groups
    ]

    for item in items:
        label_lower = item.label.lower()
        matched = False
        for idx, patterns in enumerate(group_patterns):
            if any(p in label_lower for p in patterns):
                groups[idx].items.append(item)
                matched = True
                break
        if not matched:
            fallback.items.append(item)

    # Return non-empty groups, fallback last
    result = [g for g in groups if g.items]
    if fallback.items:
        result.append(fallback)
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
    5. Group by supplier via config.yaml patterns
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

    # Step 3 — compute consumption per contenant
    target_libelles = {e.get("libelle", "") for e in contenants}
    consumption_map = _compute_consumption_from_history(history, target_libelles)

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
        )
        items.append(item)

        _log.info(
            "Stock '%s': stock=%.0f, conso=%.0f/%dj, daily=%.1f, jours=%s",
            libelle,
            item.current_stock,
            item.consumption,
            window_days,
            item.daily_consumption,
            f"{item.stock_days:.1f}" if item.stock_days else "N/A",
        )

    # Step 5 — group by supplier
    stocks_config = get_stocks_config()
    return _assign_groups(items, stocks_config)
