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
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
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


@dataclass
class OrderItem:
    """Order recommendation detail for one contenant reference."""

    label: str
    stock_days: float | None
    days_before_order: float | None   # stock_days - lead_time
    deadline: date | None             # date by which to place order
    daily_consumption: float
    bottles_per_pallet: int
    suggested_pallets: int
    suggested_qty: int                # palettes * bottles_per_pallet
    coverage_days: float | None       # suggested_qty / daily_consumption


@dataclass
class OrderRecommendation:
    """Order recommendation for one supplier."""

    supplier: str
    lead_time_days: int
    min_pallets: int
    can_split: bool
    items: list[OrderItem]
    order_deadline: date | None       # earliest deadline across items
    urgency: str                      # "critical" | "warning" | "ok"


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
                record_date = str(record.get("date", "") or "")
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
# Order recommendation
# ---------------------------------------------------------------------------


def compute_order_recommendation(
    group: StockGroup,
    ordering_cfg: dict[str, Any],
) -> OrderRecommendation | None:
    """Compute order recommendation from stock analysis + ordering config.

    Returns None if no ordering config or no items with consumption.
    """
    if not ordering_cfg:
        return None

    lead_time = int(ordering_cfg.get("lead_time_days", 0))
    min_pallets = int(ordering_cfg.get("min_order_pallets", 1))
    can_split = bool(ordering_cfg.get("can_split_references", False))
    pallet_cfg = ordering_cfg.get("pallets") or {}

    today = date.today()
    order_items: list[OrderItem] = []

    for item in group.items:
        bpp = 0
        # Find matching pallet config for this item label
        for pallet_label, pallet_data in pallet_cfg.items():
            if pallet_label == item.label:
                bpp = int(pallet_data.get("bottles_per_pallet", 0))
                break

        if bpp == 0:
            continue  # no pallet config for this reference

        days_before = None
        deadline = None
        if item.stock_days is not None:
            days_before = item.stock_days - lead_time
            deadline = today + timedelta(days=int(days_before))

        order_items.append(OrderItem(
            label=item.label,
            stock_days=item.stock_days,
            days_before_order=days_before,
            deadline=deadline,
            daily_consumption=item.daily_consumption,
            bottles_per_pallet=bpp,
            suggested_pallets=0,  # filled below
            suggested_qty=0,
            coverage_days=None,
        ))

    if not order_items:
        return None

    # --- Determine urgency from earliest deadline ---
    deadlines = [oi.days_before_order for oi in order_items
                 if oi.days_before_order is not None]
    min_days_before = min(deadlines) if deadlines else None

    if min_days_before is None:
        urgency = "ok"
        order_deadline = None
    elif min_days_before <= 0:
        urgency = "critical"
        order_deadline = today + timedelta(days=int(min_days_before))
    elif min_days_before <= 14:
        urgency = "warning"
        order_deadline = today + timedelta(days=int(min_days_before))
    else:
        urgency = "ok"
        order_deadline = today + timedelta(days=int(min_days_before))

    # --- Distribute pallets proportionally to daily consumption ---
    total_daily = sum(oi.daily_consumption for oi in order_items)
    if total_daily > 0 and len(order_items) > 1 and can_split:
        # Proportional distribution
        raw: list[float] = []
        for oi in order_items:
            raw.append((oi.daily_consumption / total_daily) * min_pallets)
        # Floor each, distribute remainder to highest fractional parts
        floored = [math.floor(r) for r in raw]
        remainder = min_pallets - sum(floored)
        fractions = [(r - f, i) for i, (r, f) in enumerate(zip(raw, floored))]
        fractions.sort(reverse=True)
        for _, idx in fractions[:remainder]:
            floored[idx] += 1
        for i, oi in enumerate(order_items):
            oi.suggested_pallets = max(floored[i], 1)  # at least 1
    elif len(order_items) == 1:
        order_items[0].suggested_pallets = min_pallets
    else:
        # Equal split
        per_item = max(min_pallets // len(order_items), 1)
        for oi in order_items:
            oi.suggested_pallets = per_item

    # Ensure total >= min_pallets
    total_suggested = sum(oi.suggested_pallets for oi in order_items)
    if total_suggested < min_pallets and order_items:
        order_items[0].suggested_pallets += min_pallets - total_suggested

    # Compute quantities and coverage
    for oi in order_items:
        oi.suggested_qty = oi.suggested_pallets * oi.bottles_per_pallet
        if oi.daily_consumption > 0:
            oi.coverage_days = oi.suggested_qty / oi.daily_consumption

    return OrderRecommendation(
        supplier=group.name,
        lead_time_days=lead_time,
        min_pallets=min_pallets,
        can_split=can_split,
        items=order_items,
        order_deadline=order_deadline,
        urgency=urgency,
    )


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


# ---------------------------------------------------------------------------
# MP / Emballages entry-point (blocking — run in thread)
# ---------------------------------------------------------------------------

def _extract_supplier_map_from_entries(
    entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Build libelle → most-recent-supplier map from MP entry history records.

    Each record has: libelle, fournisseur, date (timestamp ms).
    We keep the supplier from the most recent entry per libelle.
    """
    supplier: dict[str, str] = {}
    latest_date: dict[str, int] = {}

    for entry in entries:
        fournisseur = (entry.get("fournisseur") or "").strip()
        libelle = (entry.get("libelle") or "").strip()
        if not fournisseur or not libelle:
            continue
        # date is a unix timestamp in milliseconds
        entry_date = int(entry.get("date", 0) or 0)
        prev = latest_date.get(libelle, 0)
        if entry_date >= prev:
            supplier[libelle] = fournisseur
            latest_date[libelle] = entry_date

    return supplier


def fetch_and_compute_mp(window_days: int) -> list[StockGroup]:
    """Fetch EasyBeer data and compute stock duration for matières premières.

    1. ``GET /stock/matieres-premieres/all``  → current stock + seuil
    2. ``POST /indicateur/synthese-consommations-mp`` → consumption over period
    3. ``POST /stock/matieres-premieres/historique/entree/{cat}`` → supplier mapping
    4. Compute daily consumption and remaining stock days
    5. Group by supplier (dynamic from history, config.yaml fallback)
    """
    from common.data import get_stocks_config
    from common.easybeer._client import _dates
    from common.easybeer.history import get_mp_historique_entree
    from common.easybeer.stocks import get_all_matieres_premieres, get_synthese_consommations_mp

    # Step 1 — current stock for ALL matières premières
    all_mp = get_all_matieres_premieres()
    if not all_mp:
        _log.warning("Aucune matière première trouvée dans EasyBeer")
        return []

    # Step 2 — consumption synthesis over the period
    try:
        conso_data = get_synthese_consommations_mp(window_days)
    except Exception:
        _log.exception("Erreur appel synthese-consommations-mp")
        conso_data = {}

    # Build consumption map: idMatierePremiere → total consumption
    consumption_by_id: dict[int, float] = {}
    for cat_key in (
        "syntheseIngredient", "syntheseConditionnement",
        "syntheseDivers", "syntheseContenant",
    ):
        cat = conso_data.get(cat_key) or {}
        for el in cat.get("elements") or []:
            mp_id = el.get("idMatierePremiere")
            if mp_id is not None:
                consumption_by_id[mp_id] = float(el.get("quantite", 0) or 0)

    # Step 3 — supplier mapping from entry history
    date_debut, date_fin = _dates(window_days)
    supplier_map: dict[str, str] = {}  # libelle → fournisseur

    for cat in ("Ingredient", "Conditionnement", "Divers"):
        try:
            entries = get_mp_historique_entree(
                cat, date_debut=date_debut, date_fin=date_fin,
            )
            partial = _extract_supplier_map_from_entries(entries)
            # Merge (don't overwrite existing — first category wins for duplicates)
            for lib, sup in partial.items():
                if lib not in supplier_map:
                    supplier_map[lib] = sup
        except Exception:
            _log.warning("Erreur historique entree MP %s", cat, exc_info=True)

    _log.info(
        "MP supplier map: %d libelles → fournisseur mappés",
        len(supplier_map),
    )

    # Step 4 — build StockItem list
    items: list[StockItem] = []

    for mp in all_mp:
        mp_id = mp.get("idMatierePremiere")
        libelle = (mp.get("libelle") or "").strip()
        mp_type_code = (mp.get("type") or {}).get("code", "")

        # Skip CONTENANT type — handled by fetch_and_compute()
        if mp_type_code == "CONTENANT":
            continue

        current_stock = float(mp.get("quantiteVirtuelle", 0) or 0)
        seuil_bas = float(mp.get("seuilBas", 0) or 0)
        unite = (mp.get("unite") or {}).get("symbole", "u")
        consumption = consumption_by_id.get(mp_id, 0.0)
        daily = consumption / window_days if window_days > 0 else 0.0
        stock_days = (current_stock / daily) if daily > 0 else None

        item = StockItem(
            label=libelle,
            current_stock=current_stock,
            unit=unite,
            seuil_bas=seuil_bas,
            consumption=consumption,
            window_days=window_days,
            daily_consumption=daily,
            stock_days=stock_days,
            supplier=supplier_map.get(libelle),
        )
        items.append(item)

        if consumption > 0:
            _log.info(
                "MP '%s': stock=%.1f %s, conso=%.1f/%dj, daily=%.2f, jours=%s, fournisseur=%s",
                libelle,
                current_stock,
                unite,
                consumption,
                window_days,
                daily,
                f"{stock_days:.1f}" if stock_days else "N/A",
                item.supplier or "?",
            )

    # Step 5 — group by supplier
    stocks_config = get_stocks_config()
    return _assign_groups(items, stocks_config)
