"""
ui/_stocks_calc.py
==================
Stock duration computation for bottles — no UI, thread-safe.

Called via ``asyncio.to_thread(fetch_and_compute, window_days)`` from the
``/stocks`` page.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("ferment.stocks")

# ---------------------------------------------------------------------------
# Bottle definitions — keywords must ALL match in MP libelle (case-insensitive)
# ---------------------------------------------------------------------------

BOTTLE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Bouteille 33cl bavarian", ["bouteille", "33", "bavarian"]),
    ("Bouteille 75cl SAFT", ["bouteille", "75", "saft"]),
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class BottleStockResult:
    """Computed stock metrics for a single bottle type."""

    label: str
    id_mp: int
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

def find_bottle_mps(
    all_mps: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Search all matières premières for the tracked bottle types.

    Returns ``{label: mp_dict}`` for every bottle found.
    Match strategy: every keyword must appear in ``libelle`` (case-insensitive).
    """
    found: dict[str, dict[str, Any]] = {}
    for label, keywords in BOTTLE_KEYWORDS:
        for mp in all_mps:
            libelle = (mp.get("libelle") or "").lower()
            if all(kw.lower() in libelle for kw in keywords):
                found[label] = mp
                _log.info(
                    "MP trouvée '%s' → id=%s, stock=%.1f",
                    label,
                    mp.get("idMatierePremiere"),
                    mp.get("quantiteVirtuelle", 0),
                )
                break
        else:
            _log.warning("MP introuvable pour '%s'", label)
    return found


def compute_consumption(history: list[dict[str, Any]]) -> float:
    """Total consumption = sum of ``abs(difference)`` for negative differences."""
    total = 0.0
    for record in history:
        diff = record.get("difference", 0) or 0
        if diff < 0:
            total += abs(diff)
    return total


def compute_stock_duration(
    mp: dict[str, Any],
    history: list[dict[str, Any]],
    window_days: int,
    label: str,
) -> BottleStockResult:
    """Compute stock duration for one bottle type."""
    current_stock = float(mp.get("quantiteVirtuelle", 0) or 0)
    unit_obj = mp.get("unite") or {}
    unit = unit_obj.get("symbole", "u")
    seuil_bas = float(mp.get("seuilBas", 0) or 0)

    consumption = compute_consumption(history)
    daily = consumption / window_days if window_days > 0 else 0.0
    stock_days = (current_stock / daily) if daily > 0 else None

    return BottleStockResult(
        label=label,
        id_mp=mp.get("idMatierePremiere", 0),
        current_stock=current_stock,
        unit=unit,
        seuil_bas=seuil_bas,
        consumption=consumption,
        window_days=window_days,
        daily_consumption=daily,
        stock_days=stock_days,
    )


# ---------------------------------------------------------------------------
# Main entry-point (blocking — run in thread)
# ---------------------------------------------------------------------------

def fetch_and_compute(window_days: int) -> list[BottleStockResult]:
    """Fetch EasyBeer data and compute stock duration for tracked bottles.

    1. ``GET /stock/matieres-premieres/all`` → find bottle MP ids
    2. ``POST /stock/contenant/historique`` × N  → consumption per bottle
    3. Compute daily consumption and remaining stock days
    """
    from common.easybeer import get_all_matieres_premieres, get_contenant_historique
    from common.easybeer._client import _dates

    # Step 1 — find bottle MPs
    all_mps = get_all_matieres_premieres()
    found = find_bottle_mps(all_mps)

    if not found:
        _log.warning("Aucune bouteille trouvée dans les MP EasyBeer")
        return []

    # Step 2 & 3 — fetch history + compute per bottle
    date_debut, date_fin = _dates(window_days)
    results: list[BottleStockResult] = []

    for label, mp in found.items():
        id_mp = mp.get("idMatierePremiere")
        if not id_mp:
            continue

        history = get_contenant_historique(
            date_debut=date_debut,
            date_fin=date_fin,
            ids_matieres_premieres=[id_mp],
        )

        result = compute_stock_duration(mp, history, window_days, label)
        results.append(result)

        _log.info(
            "Stock '%s': stock=%.0f, conso=%.0f/%dj, daily=%.1f, jours=%.1f",
            label,
            result.current_stock,
            result.consumption,
            window_days,
            result.daily_consumption,
            result.stock_days or 0,
        )

    return results
