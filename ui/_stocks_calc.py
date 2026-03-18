"""
ui/_stocks_calc.py
==================
Stock autonomy computation based on finished-product SALES + BOM decomposition.

Called via ``asyncio.to_thread(fetch_and_compute_bom, window_days)`` from
the ``/stocks`` page.

Data flow
---------
1. Fetch PF autonomy (sales + stock) from EasyBeer
2. Load validated BOM (product → packaging components with qty per carton)
3. Compute daily consumption of each component from PF daily sales
4. Add virtual stock from PF stock (components locked in finished products)
5. Group by supplier (dynamic from history, config.yaml fallback)
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
    type_code: str = ""  # EasyBeer type.code (e.g. "INGREDIENT_FRUIT")
    eb_id: int | None = None  # EasyBeer idMatierePremiere (stable across renames)


@dataclass
class StockGroup:
    """A supplier group containing one or more stock items."""

    name: str
    icon: str
    items: list[StockItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class OrderItem:
    """Order recommendation detail for one contenant reference."""

    label: str
    stock_days: float | None
    days_before_order: float | None   # stock_days - lead_time
    deadline: date | None             # date by which to place order
    daily_consumption: float
    qty_per_unit: int
    suggested_units: int
    suggested_qty: int                # units * qty_per_unit
    coverage_days: float | None       # suggested_qty / daily_consumption
    min_qty: int | None = None        # optional per-reference minimum


@dataclass
class OrderRecommendation:
    """Order recommendation for one supplier."""

    supplier: str
    lead_time_days: int
    min_order: int
    can_split: bool
    items: list[OrderItem]
    order_deadline: date | None       # earliest deadline across items
    urgency: str                      # "critical" | "warning" | "ok"
    order_unit: str = "palette"       # display label for order unit
    qty_unit: str = "unités"          # display label for quantity unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assign_groups(
    items: list[StockItem],
    stocks_config: dict[str, Any],
    db_overrides: dict[str, dict] | None = None,
) -> list[StockGroup]:
    """Assign stock items to supplier groups.

    Priority:
    1. Dynamic supplier from ``item.supplier`` (extracted from history)
    2. Config matching — ``mp_types`` and/or ``patterns``:
       - Both specified → AND logic (type must match AND pattern must match)
       - Only ``mp_types`` → type code must match
       - Only ``patterns`` → pattern must appear in label
    3. Final fallback: ungrouped bucket

    If *db_overrides* is provided, the ``active`` flag from DB takes
    precedence over the YAML default.
    """
    ungrouped_label = stocks_config.get("ungrouped_label", "Autres contenants")
    db_overrides = db_overrides or {}

    # Build per-supplier matching criteria (skip inactive suppliers)
    cfg_groups = stocks_config.get("supplier_groups") or []
    cfg_matchers: list[tuple[str, str, list[str], list[str]]] = [
        (
            g["name"],
            g.get("icon", "category"),
            [p.lower() for p in g.get("patterns", [])],
            g.get("mp_types", []),
        )
        for g in cfg_groups
        if db_overrides.get(g["name"], {}).get("active", g.get("active", True))
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
            for cfg_name, cfg_icon, _, _ in cfg_matchers:
                if cfg_name.lower() == group_name.lower():
                    group_icon = cfg_icon
                    group_name = cfg_name  # use config casing
                    break

        # Priority 2: config matching (mp_types + patterns)
        if not group_name:
            label_lower = item.label.lower()
            type_code = item.type_code or ""

            for cfg_name, cfg_icon, patterns, mp_types in cfg_matchers:
                if mp_types and patterns:
                    # Both specified → AND logic
                    match = (
                        type_code in mp_types
                        and any(p in label_lower for p in patterns)
                    )
                elif mp_types:
                    match = type_code in mp_types
                elif patterns:
                    match = any(p in label_lower for p in patterns)
                else:
                    match = False

                if match:
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
    min_order = int(ordering_cfg.get("min_order",
                    ordering_cfg.get("min_order_pallets", 0)))
    can_split = bool(ordering_cfg.get("can_split_references", False))
    ref_cfg = ordering_cfg.get("references") or ordering_cfg.get("pallets") or {}
    order_unit = ordering_cfg.get("order_unit", "palette")
    qty_unit = ordering_cfg.get("qty_unit", "unités")

    today = date.today()
    order_items: list[OrderItem] = []

    for item in group.items:
        qpu = 0
        item_min_qty: int | None = None
        # Find matching reference config: try eb_id first, then name
        for ref_label, ref_data in ref_cfg.items():
            ref_eb_id = ref_data.get("eb_id")
            matched = False
            if ref_eb_id and item.eb_id and int(ref_eb_id) == item.eb_id:
                matched = True  # ID-based match (survives renames)
            elif ref_label == item.label:
                matched = True  # name-based fallback
            if matched:
                qpu = int(ref_data.get("qty_per_unit",
                          ref_data.get("bottles_per_pallet", 0)))
                if ref_data.get("min_qty"):
                    item_min_qty = int(ref_data["min_qty"])
                break

        if qpu == 0:
            continue  # no reference config for this item

        days_before = None
        deadline = None
        if item.stock_days is not None:
            days_before = item.stock_days - lead_time
            deadline = today + timedelta(days=math.floor(days_before))

        order_items.append(OrderItem(
            label=item.label,
            stock_days=item.stock_days,
            days_before_order=days_before,
            deadline=deadline,
            daily_consumption=item.daily_consumption,
            qty_per_unit=qpu,
            suggested_units=0,  # filled below
            suggested_qty=0,
            coverage_days=None,
            min_qty=item_min_qty,
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
        order_deadline = today + timedelta(days=math.floor(min_days_before))
    elif min_days_before <= 14:
        urgency = "warning"
        order_deadline = today + timedelta(days=math.floor(min_days_before))
    else:
        urgency = "ok"
        order_deadline = today + timedelta(days=math.floor(min_days_before))

    # --- Distribute units proportionally to daily consumption ---
    total_daily = sum(oi.daily_consumption for oi in order_items)
    if total_daily > 0 and len(order_items) > 1 and can_split:
        # Proportional distribution
        raw: list[float] = []
        for oi in order_items:
            raw.append((oi.daily_consumption / total_daily) * min_order)
        # Floor each, distribute remainder to highest fractional parts
        floored = [math.floor(r) for r in raw]
        remainder = min_order - sum(floored)
        fractions = [(r - f, i) for i, (r, f) in enumerate(zip(raw, floored))]
        fractions.sort(reverse=True)
        for _, idx in fractions[:remainder]:
            floored[idx] += 1
        for i, oi in enumerate(order_items):
            oi.suggested_units = max(floored[i], 1)  # at least 1
    elif len(order_items) == 1:
        order_items[0].suggested_units = min_order
    else:
        # Equal split
        per_item = max(min_order // len(order_items), 1)
        for oi in order_items:
            oi.suggested_units = per_item

    # Compute quantities and enforce per-reference minimums first
    for oi in order_items:
        oi.suggested_qty = oi.suggested_units * oi.qty_per_unit
        # Enforce per-reference minimum (e.g. Adesa labels)
        if oi.min_qty and oi.suggested_qty < oi.min_qty:
            oi.suggested_units = math.ceil(oi.min_qty / oi.qty_per_unit)
            oi.suggested_qty = oi.suggested_units * oi.qty_per_unit

    # Ensure total >= min_order (after per-ref minimums are applied)
    total_suggested = sum(oi.suggested_units for oi in order_items)
    if total_suggested < min_order and order_items:
        order_items[0].suggested_units += min_order - total_suggested
        order_items[0].suggested_qty = order_items[0].suggested_units * order_items[0].qty_per_unit

    # Compute coverage
    for oi in order_items:
        if oi.daily_consumption > 0:
            oi.coverage_days = oi.suggested_qty / oi.daily_consumption

    return OrderRecommendation(
        supplier=group.name,
        lead_time_days=lead_time,
        min_order=min_order,
        can_split=can_split,
        items=order_items,
        order_deadline=order_deadline,
        urgency=urgency,
        order_unit=order_unit,
        qty_unit=qty_unit,
    )



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



# ---------------------------------------------------------------------------
# BOM-based entry-point: sales-driven autonomy (blocking — run in thread)
# ---------------------------------------------------------------------------

def fetch_and_compute_bom(window_days: int) -> list[StockGroup]:
    """Compute stock autonomy based on finished-product SALES + BOM decomposition.

    Instead of using production-based consumption (synthese-consommations-mp),
    this function:
    1. Fetches finished product autonomy (sales + stock) from EasyBeer
    2. Loads the validated BOM (product → components with qty per carton)
    3. Computes daily consumption of each component from PF daily sales
    4. Adds virtual stock from PF stock (components locked in finished products)
    5. Groups by supplier using the existing logic

    Formula per component:
        daily_consumption = Σ (PF_daily_sales × qty_per_carton)
        virtual_pf_stock  = Σ (PF_stock × qty_per_carton)
        total_stock       = raw_stock + virtual_pf_stock
        autonomy_days     = total_stock / daily_consumption
    """
    import re

    from common.data import get_stocks_config
    from common.easybeer.products import get_all_products
    from common.easybeer.stocks import get_all_matieres_premieres, get_autonomie_stocks
    from common.product_bom import get_bom_lookup
    from common.supplier_config import get_all_supplier_overrides

    # ── Step 1: Fetch finished product autonomy ──
    autonomie_data = get_autonomie_stocks(window_days)
    pf_list = autonomie_data.get("produits") or []
    _log.info("BOM calc: %d produits finis dans l'autonomie", len(pf_list))

    # ── Step 2: Build PF lookup by idProduit ──
    # autonomie returns libelle only (no idProduit), so we map via get_all_products
    all_products = get_all_products()
    label_to_pid: dict[str, int] = {}
    for p in all_products:
        lib = (p.get("libelle") or "").strip().lower()
        pid = p.get("idProduit")
        if lib and pid:
            label_to_pid[lib] = pid

    # Parse PF data: for each PF, derive daily_sales and stock
    # Key = (idProduit, format_code)
    pf_data: dict[tuple[int, str], dict] = {}

    for pf in pf_list:
        pf_label = (pf.get("libelle") or "").strip()
        autonomie = float(pf.get("autonomie") or 0)
        stock = float(pf.get("quantiteVirtuelle") or 0)

        if not pf_label:
            continue

        # Match to idProduit
        pid = label_to_pid.get(pf_label.lower())
        if not pid:
            # Try partial match (autonomie labels may include format info)
            for lab, p_id in label_to_pid.items():
                if lab in pf_label.lower() or pf_label.lower() in lab:
                    pid = p_id
                    break

        if not pid:
            _log.warning("BOM calc: PF '%s' non trouvé dans les produits", pf_label)
            continue

        # Derive format from PF label or volume
        # Try to find format like "12x33" in the label
        fmt_match = re.search(r"(\d+)\s*[x×]\s*(\d+)", pf_label)
        if fmt_match:
            format_code = f"{fmt_match.group(1)}x{fmt_match.group(2)}"
        else:
            # Fallback: try to derive from volume field
            vol = float(pf.get("volume") or 0)
            if vol > 0:
                # Common: volume in hL, guess format from it
                format_code = "unknown"
            else:
                format_code = "unknown"

        daily_sales = stock / autonomie if autonomie > 0 else 0.0

        key = (pid, format_code)
        if key in pf_data:
            # Same product-format seen twice: aggregate
            pf_data[key]["daily_sales"] += daily_sales
            pf_data[key]["stock"] += stock
        else:
            pf_data[key] = {
                "label": pf_label,
                "daily_sales": daily_sales,
                "stock": stock,
            }

        _log.info(
            "BOM calc PF: '%s' pid=%d fmt=%s → ventes=%.1f/j, stock=%.0f",
            pf_label, pid, format_code, daily_sales, stock,
        )

    # ── Step 3: Load BOM lookup and MP stock ──
    bom_lookup = get_bom_lookup()  # {id_mp: [{id_produit, format_code, qty_per_unit, ...}]}
    all_mp = get_all_matieres_premieres()

    # Build MP stock map
    mp_stock: dict[int, dict] = {}
    for mp in all_mp:
        mp_id = mp.get("idMatierePremiere")
        if not mp_id:
            continue
        mp_stock[mp_id] = {
            "label": (mp.get("libelle") or "").strip(),
            "stock": float(mp.get("quantiteVirtuelle") or 0),
            "seuil_bas": float(mp.get("seuilBas") or 0),
            "unit": (mp.get("unite") or {}).get("symbole", "u"),
            "type_code": (mp.get("type") or {}).get("code", ""),
        }

    # ── Step 4: Build supplier map (one batch, not per-component) ──
    from common.easybeer.history import get_mp_historique_entree
    from common.easybeer._client import _dates

    supplier_map: dict[str, str] = {}  # mp_label → fournisseur
    try:
        date_debut, date_fin = _dates(window_days)
        for cat in ("Conditionnement", "Ingredient", "Divers"):
            try:
                hist_entries = get_mp_historique_entree(
                    cat, date_debut=date_debut, date_fin=date_fin,
                )
                partial = _extract_supplier_map_from_entries(hist_entries)
                for lib, sup in partial.items():
                    if lib not in supplier_map:
                        supplier_map[lib] = sup
            except Exception:
                _log.warning("Erreur historique entree MP %s", cat, exc_info=True)
    except Exception:
        _log.warning("Erreur récup dates pour historique", exc_info=True)

    _log.info("BOM supplier map: %d libellés → fournisseur", len(supplier_map))

    # ── Step 5: Compute autonomy per component ──
    items: list[StockItem] = []

    for id_mp, bom_entries in bom_lookup.items():
        mp_info = mp_stock.get(id_mp)
        if not mp_info:
            _log.warning("BOM calc: MP id=%d non trouvée dans EasyBeer", id_mp)
            continue

        daily_consumption = 0.0
        virtual_pf_stock = 0.0
        contributing_pfs: list[str] = []

        for entry in bom_entries:
            pf_key = (entry["id_produit"], entry["format_code"])
            pf = pf_data.get(pf_key)

            if not pf:
                # Try matching with "unknown" format fallback
                for k, v in pf_data.items():
                    if k[0] == entry["id_produit"]:
                        pf = v
                        break

            if not pf:
                continue

            qty = entry["qty_per_unit"]
            daily_consumption += pf["daily_sales"] * qty
            virtual_pf_stock += pf["stock"] * qty
            contributing_pfs.append(entry.get("product_label", ""))

        raw_stock = mp_info["stock"]
        total_stock = raw_stock + virtual_pf_stock
        stock_days = total_stock / daily_consumption if daily_consumption > 0 else None

        # Supplier: from batch history map
        supplier = supplier_map.get(mp_info["label"])

        item = StockItem(
            label=mp_info["label"],
            current_stock=total_stock,
            unit=mp_info["unit"],
            seuil_bas=mp_info["seuil_bas"],
            consumption=daily_consumption * window_days,
            window_days=window_days,
            daily_consumption=daily_consumption,
            stock_days=stock_days,
            supplier=supplier,
            type_code=mp_info["type_code"],
            eb_id=id_mp,
        )
        items.append(item)

        _log.info(
            "BOM calc MP '%s': raw=%.0f + pf_virtual=%.0f = %.0f, "
            "conso=%.1f/j, jours=%s, PFs=%s",
            mp_info["label"],
            raw_stock,
            virtual_pf_stock,
            total_stock,
            daily_consumption,
            f"{stock_days:.1f}" if stock_days else "N/A",
            ", ".join(set(contributing_pfs)) or "aucun",
        )

    # ── Step 5: Group by supplier ──
    stocks_config = get_stocks_config()
    db_over = get_all_supplier_overrides()
    groups = _assign_groups(items, stocks_config, db_overrides=db_over)

    # Add info about BOM source
    if not bom_lookup:
        warn = (
            "Aucune nomenclature validée. Configurez les nomenclatures "
            "sur la page Nomenclatures pour activer le calcul basé sur les ventes."
        )
        for g in groups:
            g.warnings.append(warn)

    return groups
