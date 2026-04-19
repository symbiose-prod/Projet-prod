"""
pages/_stocks_calc.py
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
    current_stock: float  # stock brut (quantité physique en entrepôt)
    unit: str             # "u"
    seuil_bas: float
    consumption: float    # total consumed over period (unit MP)
    window_days: int
    daily_consumption: float
    stock_days: float | None  # current_stock / daily_consumption, or None
    supplier: str | None = None  # dynamically extracted from history
    type_code: str = ""  # EasyBeer type.code (e.g. "INGREDIENT_FRUIT")
    eb_id: int | None = None  # EasyBeer idMatierePremiere (stable across renames)
    max_recipe_qty_7200: float | None = None  # qté MP nécessaire pour la recette la plus gourmande à 7200L
    max_recipe_qty_5200: float | None = None  # idem pour 5200L
    max_recipe_product: str = ""  # nom du produit de la recette la plus gourmande


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
    1. Dynamic supplier from ``item.supplier`` (extracted from EasyBeer
       purchase history over 365 days — set in step 4 of fetch_and_compute_bom)
    2. Config fallback — ``mp_types`` and/or ``patterns``:
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

        # Priority 2: config fallback (mp_types + patterns)
        if not group_name:
            label_lower = item.label.lower()
            type_code = item.type_code or ""

            for cfg_name, cfg_icon, patterns, mp_types in cfg_matchers:
                if mp_types and patterns:
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
    else:
        order_deadline = today + timedelta(days=math.floor(min_days_before))
        if min_days_before <= 0:
            urgency = "critical"
        elif min_days_before <= 14:
            urgency = "warning"
        else:
            urgency = "ok"

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
    """Compute stock autonomy from sales (DataFrame Accueil) × recettes.

    INGRÉDIENTS : conso = Volume vendu (hL) × 100 × (qty_recette / vol_recette)
    PACKAGING   : conso = Quantité vendue × qty_per_carton (BOM)
    """
    import re
    from concurrent.futures import ThreadPoolExecutor

    from common.data import get_stocks_config
    from common.easybeer._client import _dates, _safe_list
    from common.easybeer.history import get_mp_historique_entree
    from common.easybeer.products import get_all_products
    from common.easybeer.stocks import get_all_matieres_premieres, get_autonomie_stocks
    from common.product_bom import get_bom_lookup
    from common.supplier_config import get_all_supplier_overrides

    # ── Step 0: Parallel fetch of all EasyBeer data ──
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_autonomie = pool.submit(get_autonomie_stocks, window_days)
        f_products = pool.submit(get_all_products)
        f_mp = pool.submit(get_all_matieres_premieres)
        date_debut_365, date_fin_365 = _dates(365)
        f_hist = {
            cat: pool.submit(get_mp_historique_entree, cat, date_debut=date_debut_365, date_fin=date_fin_365)
            for cat in ("Conditionnement", "Ingredient", "Divers")
        }

    # ── Step 1: Build sales data from autonomie JSON ──
    # Dans le JSON autonomie EasyBeer :
    #   volume        = Volume VENDU (hL) sur la période
    #   volumeVirtuel = Volume en STOCK (hL)
    #   quantite      = Cartons VENDUS sur la période
    #   quantiteVirtuelle = Cartons en STOCK
    autonomie_data = f_autonomie.result() or {}
    # Parsing défensif : _safe_list gère aussi le cas "produits: null" renvoyé
    # par EasyBeer (plutôt que la clé absente), qui crasherait sur itération.
    pf_list = _safe_list(autonomie_data, "produits", "autonomie-stocks")
    if not pf_list:
        _log.warning("BOM calc: aucun produit fini dans l'autonomie EasyBeer")
        return []
    _log.info("BOM calc: %d produits finis dans l'autonomie (%dj)", len(pf_list), window_days)

    all_products = f_products.result() or []
    label_to_pid: dict[str, int] = {}
    for p in all_products:
        lib = (p.get("libelle") or "").strip().lower()
        pid = p.get("idProduit")
        if lib and pid:
            label_to_pid[lib] = pid

    # Agréger ventes par produit (tous formats) pour les ingrédients
    # pf_sales[pid] = {label, ventes_hl, qty_vendue}
    pf_sales: dict[int, dict] = {}
    # pf_data par format pour le packaging (BOM)
    pf_data: dict[tuple[int, str], dict] = {}

    for pf in pf_list:
        pf_label = (pf.get("libelle") or "").strip()
        if not pf_label:
            continue

        pid = label_to_pid.get(pf_label.lower())
        if not pid:
            for lab, p_id in label_to_pid.items():
                if lab in pf_label.lower() or pf_label.lower() in lab:
                    pid = p_id
                    break
        if not pid:
            _log.warning("BOM calc: PF '%s' non trouvé dans les produits", pf_label)
            continue

        # volume = ventes hL sur la période, quantite = cartons vendus
        ventes_hl = float(pf.get("volume") or 0)
        qty_vendue = float(pf.get("quantite") or 0)

        # Agréger par produit (tous formats) pour les ingrédients
        if pid in pf_sales:
            pf_sales[pid]["ventes_hl"] += ventes_hl
            pf_sales[pid]["qty_vendue"] += qty_vendue
        else:
            pf_sales[pid] = {
                "label": pf_label,
                "ventes_hl": ventes_hl,
                "qty_vendue": qty_vendue,
            }

        # Par format pour le packaging — parsing défensif (null-safe)
        for sp in _safe_list(pf, "stocksProduits", "autonomie-stocks"):
            sp_label = sp.get("libelle") or ""
            fmt_match = re.search(r"(\d+)\s*[x×]\s*(\d+)", sp_label)
            if fmt_match:
                format_code = f"{fmt_match.group(1)}x{fmt_match.group(2)}"
            else:
                lot = sp.get("lot") or {}
                cont = sp.get("contenant") or {}
                lot_qty = int(lot.get("quantite", 0) or 0)
                contenance = float(cont.get("contenance", 0) or 0)
                format_code = f"{lot_qty}x{int(contenance * 100)}" if lot_qty and contenance else "unknown"

            sp_qty = float(sp.get("quantite") or 0)  # cartons vendus ce format
            sp_vol = float(sp.get("volume") or 0)  # volume vendu ce format

            key = (pid, format_code)
            if key in pf_data:
                pf_data[key]["daily_sales"] += sp_qty / max(window_days, 1)
            else:
                pf_data[key] = {
                    "label": pf_label,
                    "daily_sales": sp_qty / max(window_days, 1),
                }

    _log.info(
        "Stocks calc: %d produits, %d formats depuis autonomie (%dj)",
        len(pf_sales), len(pf_data), window_days,
    )

    # ── Step 3: Load BOM lookup and MP stock ──
    bom_lookup = get_bom_lookup()  # {id_mp: [{id_produit, format_code, qty_per_unit, ...}]}
    all_mp = f_mp.result() or []

    # Build MP stock map
    mp_stock: dict[int, dict] = {}
    for mp in all_mp:
        mp_id = mp.get("idMatierePremiere")
        if not mp_id:
            continue
        _unite = mp.get("unite")
        _type = mp.get("type")
        mp_stock[mp_id] = {
            "label": (mp.get("libelle") or "").strip(),
            "stock": float(mp.get("quantiteVirtuelle") or 0),
            "seuil_bas": float(mp.get("seuilBas") or 0),
            "unit": _unite.get("symbole", "u") if isinstance(_unite, dict) else "u",
            "type_code": _type.get("code", "") if isinstance(_type, dict) else "",
        }

    # Enrichir mp_stock avec les bouteilles (CONTENANT séparés dans EasyBeer)
    try:
        from common.easybeer.stocks import get_bottle_stock
        from common.product_bom import get_all_bom
        bottle_stock = get_bottle_stock()
        # Charger les libellés MP depuis le BOM complet
        _bom_mp_labels: dict[int, str] = {}
        for entry in get_all_bom():
            _bom_mp_labels.setdefault(entry["id_mp"], entry.get("mp_label", ""))
        for cont_id, qty in bottle_stock.items():
            int_id = int(cont_id)
            if int_id not in mp_stock:
                mp_stock[int_id] = {
                    "label": _bom_mp_labels.get(int_id, f"Contenant #{int_id}"),
                    "stock": float(qty),
                    "seuil_bas": 0,
                    "unit": "u",
                    "type_code": "CONTENANT",
                }
        _log.info("Bottle stock: %d contenants ajoutés à mp_stock", len(bottle_stock))
    except Exception:
        _log.debug("Erreur chargement stock bouteilles", exc_info=True)

    # ── Step 4: Build supplier map (one batch, not per-component) ──
    # Use a 365-day window (not window_days) to catch suppliers for MP
    # that haven't been ordered recently within the analysis period.
    supplier_map: dict[str, str] = {}  # mp_label → fournisseur
    for cat, fut in f_hist.items():
        try:
            hist_entries = fut.result()
            partial = _extract_supplier_map_from_entries(hist_entries)
            for lib, sup in partial.items():
                if lib not in supplier_map:
                    supplier_map[lib] = sup
        except Exception:
            _log.warning("Erreur historique entree MP %s", cat, exc_info=True)

    # Build id-based supplier map (survives MP label renames)
    label_to_id: dict[str, int] = {v["label"]: k for k, v in mp_stock.items()}
    supplier_map_by_id: dict[int, str] = {}
    for label, supplier in supplier_map.items():
        mp_id = label_to_id.get(label)
        if mp_id:
            supplier_map_by_id[mp_id] = supplier

    _log.info(
        "BOM supplier map: %d libellés, %d ids → fournisseur",
        len(supplier_map), len(supplier_map_by_id),
    )

    # ── Step 5: Build recipe consumption map from ventes × recettes ──
    #
    # Pour chaque produit vendu, on charge la recette et on calcule
    # la consommation de chaque ingrédient sur la période :
    #   conso_mp = Volume vendu (hL) × 100 × (qty_recette / volume_recette)
    #
    # recipe_conso[id_mp] = [(product_label, conso_periode, ventes_hl)]
    from common.easybeer.products import get_product_detail

    _recipe_cache: dict[int, dict] = {}
    recipe_conso: dict[int, list[tuple[str, float, float]]] = {}
    max_recipe_7200: dict[int, tuple[float, str]] = {}  # id_mp → (qty, product_label)
    max_recipe_5200: dict[int, tuple[float, str]] = {}

    # Construire un mapping dérivé → parent via le flavor_map
    # Les NIKO, Inter, Water n'ont pas de recette propre — on utilise celle du Symbiose parent
    from common.bom_detection import _clean_eb_label
    from common.data import read_flavor_map

    fm = read_flavor_map()
    _label_to_canon: dict[str, str] = {}
    if not fm.empty:
        for _, row in fm.iterrows():
            name = str(row.get("name", "")).strip().lower()
            canon = str(row.get("canonical", "")).strip()
            if name and canon:
                _label_to_canon[name] = canon

    def _find_parent_recipe(child_pid: int, child_label: str) -> dict | None:
        """Trouve la recette du produit Symbiose parent pour un produit dérivé."""
        child_canon = _label_to_canon.get(child_label.lower()) or _label_to_canon.get(_clean_eb_label(child_label).lower())
        if not child_canon:
            return None
        # Chercher un produit avec recette qui a le même goût canonique
        for other_pid, other_info in pf_sales.items():
            if other_pid == child_pid:
                continue
            other_canon = _label_to_canon.get(other_info["label"].lower()) or _label_to_canon.get(_clean_eb_label(other_info["label"]).lower())
            if other_canon == child_canon:
                if other_pid not in _recipe_cache:
                    _recipe_cache[other_pid] = get_product_detail(other_pid)
                parent = _recipe_cache[other_pid]
                if parent.get("recettes"):
                    return parent
        return None

    for pid, pf_info in pf_sales.items():
        pf_label = pf_info["label"]
        ventes_hl = pf_info["ventes_hl"]
        if ventes_hl <= 0:
            continue
        try:
            if pid not in _recipe_cache:
                _recipe_cache[pid] = get_product_detail(pid)
            detail = _recipe_cache[pid]
            recettes = detail.get("recettes") or []
            if not recettes:
                # Produit dérivé sans recette → utiliser la recette du parent
                parent = _find_parent_recipe(pid, pf_label)
                if parent:
                    recettes = parent.get("recettes") or []
                    _log.info("Stocks: '%s' → recette parent '%s'", pf_label, parent.get("libelle", "?"))
            if not recettes:
                _log.debug("Stocks: '%s' sans recette, ignoré", pf_label)
                continue
            recette = recettes[0]
            vol_recette = float(recette.get("volumeRecette") or 0)
            if vol_recette <= 0:
                continue
            for ing in recette.get("ingredients") or []:
                ing_mp = ing.get("matierePremiere") or {}
                id_mp = ing_mp.get("idMatierePremiere")
                if not id_mp:
                    continue
                qty_recipe = float(ing.get("quantite") or 0)
                if qty_recipe <= 0:
                    continue
                # Conso sur la période = Volume vendu (hL) × 100 (L/hL) × qty/L
                qty_per_litre = qty_recipe / vol_recette
                conso_periode = ventes_hl * 100 * qty_per_litre
                recipe_conso.setdefault(id_mp, []).append(
                    (pf_label, conso_periode, ventes_hl)
                )
                # Cuves : quantité nécessaire pour la recette la plus gourmande
                qty_7200 = qty_per_litre * 7200
                qty_5200 = qty_per_litre * 5200
                prev_7200 = max_recipe_7200.get(id_mp)
                if prev_7200 is None or qty_7200 > prev_7200[0]:
                    max_recipe_7200[id_mp] = (qty_7200, pf_label)
                prev_5200 = max_recipe_5200.get(id_mp)
                if prev_5200 is None or qty_5200 > prev_5200[0]:
                    max_recipe_5200[id_mp] = (qty_5200, pf_label)
        except Exception:
            _log.debug("Recipe lookup failed for product %d", pid, exc_info=True)

    _log.info(
        "Recipe consumption map: %d MP calculées depuis %d produits",
        len(recipe_conso), len(pf_sales),
    )

    # ── Step 6: Compute autonomy per component ──
    items: list[StockItem] = []

    # Ensemble de tous les id_mp à traiter (recettes + BOM packaging)
    all_mp_ids = set(recipe_conso.keys()) | set(bom_lookup.keys())

    for id_mp in all_mp_ids:
        mp_info = mp_stock.get(id_mp)
        if not mp_info:
            continue

        type_code = mp_info["type_code"]
        is_ingredient = type_code.startswith("INGREDIENT")

        if is_ingredient and id_mp in recipe_conso:
            # ── INGRÉDIENTS : conso depuis recette × ventes hL ──
            entries = recipe_conso[id_mp]
            conso_periode = sum(e[1] for e in entries)  # conso totale sur la période
            daily_consumption = conso_periode / max(window_days, 1)
            sales_hl_total = sum(e[2] for e in entries)
            contributing_pfs = [e[0] for e in entries]
        elif id_mp in bom_lookup:
            # ── PACKAGING : conso depuis BOM × cartons/jour ──
            daily_consumption = 0.0
            contributing_pfs = []
            _seen_pf_fmt: set[tuple[int, str]] = set()
            for entry in bom_lookup[id_mp]:
                pid = entry["id_produit"]
                fmt = entry.get("format_code", "")
                pf_key = (pid, fmt)
                if pf_key in _seen_pf_fmt:
                    continue
                _seen_pf_fmt.add(pf_key)
                pf = pf_data.get((pid, entry["format_code"]))
                if not pf:
                    for k, v in pf_data.items():
                        if k[0] == pid:
                            pf = v
                            break
                if not pf:
                    continue
                qty = float(entry.get("qty_per_unit") or 0)
                if qty <= 0:
                    continue
                daily_consumption += pf["daily_sales"] * qty
                contributing_pfs.append(entry.get("product_label", ""))
        else:
            continue

        raw_stock = mp_info["stock"]
        stock_days = raw_stock / daily_consumption if daily_consumption > 0 else None

        # Cuves
        _r7200 = max_recipe_7200.get(id_mp)
        max_recipe_qty = _r7200[0] if _r7200 else None
        max_recipe_product = _r7200[1] if _r7200 else ""
        _r5200 = max_recipe_5200.get(id_mp)
        max_recipe_qty_5200 = _r5200[0] if _r5200 else None

        # Supplier: prefer id-based match (survives renames), then label fallback
        supplier = supplier_map_by_id.get(id_mp) or supplier_map.get(mp_info["label"])

        item = StockItem(
            label=mp_info["label"],
            current_stock=raw_stock,
            unit=mp_info["unit"],
            seuil_bas=mp_info["seuil_bas"],
            consumption=daily_consumption * window_days,
            window_days=window_days,
            daily_consumption=daily_consumption,
            stock_days=stock_days,
            supplier=supplier,
            type_code=mp_info["type_code"],
            eb_id=id_mp,
            max_recipe_qty_7200=max_recipe_qty,
            max_recipe_qty_5200=max_recipe_qty_5200,
            max_recipe_product=max_recipe_product,
        )
        items.append(item)

        _log.info(
            "BOM calc MP '%s': stock=%.0f, conso=%.1f/j, jours=%s, PFs=%s",
            mp_info["label"],
            raw_stock,
            daily_consumption,
            f"{stock_days:.1f}" if stock_days else "N/A",
            ", ".join(set(contributing_pfs)) or "aucun",
        )

    # ── Step 6: Detect PF with sales but no validated BOM ──
    # Compare by product id only (pf_data keys have format "unknown"
    # while BOM keys have real formats like "12x33")
    bom_pids: set[int] = set()
    for entries in bom_lookup.values():
        for e in entries:
            bom_pids.add(e["id_produit"])

    missing_pf = [
        pf_data[k]["label"]
        for k in pf_data
        if k[0] not in bom_pids and pf_data[k]["daily_sales"] > 0
    ]
    if missing_pf:
        _log.warning(
            "BOM calc: %d PF sans nomenclature validée : %s",
            len(missing_pf), ", ".join(missing_pf),
        )

    # ── Step 7: Group by supplier ──
    stocks_config = get_stocks_config()
    db_over = get_all_supplier_overrides()
    groups = _assign_groups(items, stocks_config, db_overrides=db_over)

    # Add warnings
    if not bom_lookup:
        warn = (
            "Aucune nomenclature validée. Configurez les nomenclatures "
            "sur la page Nomenclatures pour activer le calcul basé sur les ventes."
        )
        for g in groups:
            g.warnings.append(warn)

    if missing_pf:
        labels = ", ".join(sorted(missing_pf))
        warn = (
            f"{len(missing_pf)} produit(s) sans nomenclature validée "
            f"(non inclus dans le calcul) : {labels}. "
            "Configurez leur nomenclature sur la page Nomenclatures."
        )
        for g in groups:
            g.warnings.append(warn)

    return groups
