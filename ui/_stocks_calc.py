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
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("ferment.stocks")

# ---------------------------------------------------------------------------
# Bottle definitions — match on the ``libelle`` field returned by
# ``GET /stock/bouteilles`` (e.g. "Bouteille - 0.33L").
# The same string appears in the ``stock`` field of history records.
# ---------------------------------------------------------------------------

BOTTLE_TARGETS: list[tuple[str, str]] = [
    ("Bouteille 33cl", "Bouteille - 0.33L"),
    ("Bouteille 75cl SAFT", "Bouteille 75cl SAFT - 0.75L"),
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class BottleStockResult:
    """Computed stock metrics for a single bottle type."""

    label: str
    eb_libelle: str  # EasyBeer libelle (e.g. "Bouteille - 0.33L")
    current_stock: float
    unit: str
    seuil_bas: float
    consumption: float  # total consumed over period
    window_days: int
    daily_consumption: float  # consumption / window_days
    stock_days: float | None  # current_stock / daily_consumption, or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_bottles_in_consolidation(
    consolidation: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Find target bottles in the consolidation tree from GET /stock/bouteilles.

    Returns ``{eb_libelle: consolidation_entry}`` for each target found.
    """
    children = consolidation.get("consolidationsFilles") or []
    found: dict[str, dict[str, Any]] = {}

    target_libelles = {eb_libelle for _, eb_libelle in BOTTLE_TARGETS}

    for entry in children:
        libelle = entry.get("libelle", "")
        if libelle in target_libelles:
            found[libelle] = entry
            _log.info(
                "Contenant trouvé '%s' → id=%s, stock=%s, seuilBas=%s",
                libelle,
                entry.get("id"),
                entry.get("quantiteVirtuelle"),
                entry.get("seuilBas"),
            )

    for _, eb_libelle in BOTTLE_TARGETS:
        if eb_libelle not in found:
            _log.warning("Contenant introuvable : '%s'", eb_libelle)

    return found


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


# ---------------------------------------------------------------------------
# Main entry-point (blocking — run in thread)
# ---------------------------------------------------------------------------

def fetch_and_compute(window_days: int) -> list[BottleStockResult]:
    """Fetch EasyBeer data and compute stock duration for tracked bottles.

    1. ``GET /stock/bouteilles``     → find bottles, get current stock
    2. ``POST /stock/contenant/historique`` → get all movements over period
    3. Match history to bottles by ``stock``/``libelle`` field
    4. Compute daily consumption and remaining stock days
    """
    from common.easybeer import get_contenant_historique, get_stock_bouteilles
    from common.easybeer._client import _dates

    # Step 1 — get current stock levels for all bottles
    consolidation = get_stock_bouteilles()
    found = _find_bottles_in_consolidation(consolidation)

    if not found:
        _log.warning("Aucun contenant bouteille trouvé dans EasyBeer")
        return []

    # Step 2 — fetch movement history for the period
    date_debut, date_fin = _dates(window_days)
    history = get_contenant_historique(
        date_debut=date_debut,
        date_fin=date_fin,
    )

    # Step 3 — compute consumption per bottle type
    target_libelles = {eb_libelle for _, eb_libelle in BOTTLE_TARGETS}
    consumption_map = _compute_consumption_from_history(history, target_libelles)

    # Step 4 — build results
    results: list[BottleStockResult] = []

    for display_label, eb_libelle in BOTTLE_TARGETS:
        entry = found.get(eb_libelle)
        if not entry:
            continue

        current_stock = float(entry.get("quantiteVirtuelle", 0) or 0)
        seuil_bas = float(entry.get("seuilBas", 0) or 0)
        consumption = consumption_map.get(eb_libelle, 0.0)
        daily = consumption / window_days if window_days > 0 else 0.0
        stock_days = (current_stock / daily) if daily > 0 else None

        result = BottleStockResult(
            label=display_label,
            eb_libelle=eb_libelle,
            current_stock=current_stock,
            unit="u",
            seuil_bas=seuil_bas,
            consumption=consumption,
            window_days=window_days,
            daily_consumption=daily,
            stock_days=stock_days,
        )
        results.append(result)

        _log.info(
            "Stock '%s': stock=%.0f, conso=%.0f/%dj, daily=%.1f, jours=%s",
            display_label,
            result.current_stock,
            result.consumption,
            window_days,
            result.daily_consumption,
            f"{result.stock_days:.1f}" if result.stock_days else "N/A",
        )

    return results
