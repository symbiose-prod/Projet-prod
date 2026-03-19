"""
ui/_planification_calc.py
=========================
Computation engine for the /planification page.

Fetches planned brassins from EasyBeer and computes component needs
(ingredients + packaging) based on BOM decomposition. Thread-safe,
no NiceGUI UI code.

Called via ``asyncio.to_thread(fetch_planning_data, days_ahead)``
from ``ui/planification.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("ferment.planification")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ConditioningLine:
    """One conditioning line of a planned brassin."""

    id_planif: int              # idBrassinPlanificationProduction
    product_label: str          # e.g. "Kéfir Mangue Passion - 0.0°"
    id_produit: int
    contenant_label: str        # e.g. "Carton de 12 Bouteilles - 0.33L"
    quantity: int               # number of packs/cartons
    volume: float               # total volume in liters
    id_contenant: int
    id_lot: int


@dataclass
class PlannedBrassin:
    """A planned brassin (etat=PLANIFIE) from EasyBeer."""

    id_brassin: int
    code: str                   # brassin nom (e.g. "KMA20032026")
    product_label: str          # main product label
    id_produit: int
    volume: float               # total volume in liters
    date_debut: str             # ISO date or epoch
    date_conditionnement: str   # ISO date or epoch
    conditioning: list[ConditioningLine] = field(default_factory=list)
    ingredients: list[dict] = field(default_factory=list)  # raw from API
    packaging: list[dict] = field(default_factory=list)    # from matieresPremieresPlanificationConditionnement


@dataclass
class ComponentNeed:
    """Stock impact for one raw material component."""

    id_mp: int
    label: str
    unit: str
    current_stock: float        # current MP stock from EasyBeer
    total_needed: float         # sum of needs from all planned brassins
    stock_after: float          # current_stock - total_needed
    supplier: str | None = None
    type_code: str = ""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_epoch_to_iso(val: Any) -> str:
    """Convert epoch millis (int) or ISO string to display string."""
    if isinstance(val, (int, float)) and val > 0:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(val / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    if isinstance(val, str) and val:
        return val[:10]  # "2026-03-20T..."  → "2026-03-20"
    return ""


def _parse_brassin(raw: dict) -> PlannedBrassin:
    """Parse raw EasyBeer brassin dict into PlannedBrassin."""
    produit = raw.get("produit") or {}

    # Parse conditioning lines from planificationsProductions
    lines: list[ConditioningLine] = []
    for pp in raw.get("planificationsProductions") or []:
        pp_produit = pp.get("produit") or {}
        lines.append(ConditioningLine(
            id_planif=pp.get("idBrassinPlanificationProduction", 0),
            product_label=pp_produit.get("libelle") or pp_produit.get("nom") or "",
            id_produit=pp_produit.get("idProduit", 0),
            contenant_label=pp.get("conditionnement") or "",
            quantity=int(pp.get("quantite") or 0),
            volume=float(pp.get("volume") or 0),
            id_contenant=pp.get("idContenant", 0),
            id_lot=pp.get("idLot", 0),
        ))

    # Parse ingredients
    ingredients: list[dict] = []
    for ing in raw.get("ingredients") or []:
        mp = ing.get("matierePremiere") or {}
        ingredients.append({
            "id_mp": mp.get("idMatierePremiere", 0),
            "label": mp.get("libelle") or "",
            "total_qty": float(ing.get("quantite") or 0),  # already total for full brassin
            "unit": (mp.get("unite") or {}).get("symbole", ""),
        })

    # Parse packaging from matieresPremieresPlanificationConditionnement
    # (EasyBeer pre-calculates exact packaging needs per conditioning line)
    packaging: list[dict] = []
    for m in raw.get("matieresPremieresPlanificationConditionnement") or []:
        mp_inner = m.get("matierePremiere") or {}
        id_mp = m.get("idMatierePremiere") or mp_inner.get("idMatierePremiere")
        label = m.get("libelle") or mp_inner.get("libelle") or ""
        qty = float(m.get("quantite") or 0)
        if id_mp and qty > 0:
            packaging.append({
                "id_mp": id_mp,
                "label": label,
                "total_qty": qty,
            })

    return PlannedBrassin(
        id_brassin=raw.get("idBrassin", 0),
        code=raw.get("nom") or "",
        product_label=produit.get("libelle") or produit.get("nom") or "",
        id_produit=produit.get("idProduit", 0),
        volume=float(raw.get("volume") or 0),
        date_debut=_parse_epoch_to_iso(
            raw.get("dateDebutPlanificationFormulaire")
            or raw.get("dateDebutCalendrier")
            or raw.get("dateDebutFormulaire")
        ),
        date_conditionnement=_parse_epoch_to_iso(
            raw.get("dateConditionnementPrevue") or ""
        ),
        conditioning=lines,
        ingredients=ingredients,
        packaging=packaging,
    )


# ---------------------------------------------------------------------------
# Main public functions (blocking — call via asyncio.to_thread)
# ---------------------------------------------------------------------------

def fetch_planning_data(
    days_ahead: int = 90,
) -> tuple[list[PlannedBrassin], list[ComponentNeed]]:
    """Fetch planned brassins and compute component needs.

    Returns ``(brassins, component_needs)`` where component_needs
    lists every raw material impacted by the planned production,
    with current stock and how much will be consumed.
    """
    from common.easybeer.brassins import get_brassins_planifies, get_brassin_detail
    from common.easybeer.stocks import get_all_matieres_premieres

    # ── 1. Fetch planned brassins (summary) ──
    raw_list = get_brassins_planifies(days_ahead)
    if not raw_list:
        _log.info("No planned brassins found (horizon %dj)", days_ahead)
        return [], []

    # ── 2. Fetch full detail for each (includes ingredients + conditioning) ──
    brassins: list[PlannedBrassin] = []
    for raw in raw_list:
        bid = raw.get("idBrassin")
        if not bid:
            continue
        try:
            detail = get_brassin_detail(bid)
            brassins.append(_parse_brassin(detail))
        except Exception:
            _log.warning("Erreur fetch detail brassin %s", bid, exc_info=True)
            # Fallback: parse the summary (less data)
            brassins.append(_parse_brassin(raw))

    _log.info(
        "Parsed %d planned brassins: %s",
        len(brassins),
        ", ".join(f"{b.code} ({b.volume:.0f}L)" for b in brassins),
    )

    # ── 3. Compute component needs ──
    # Aggregate needs per MP id
    needs_by_mp: dict[int, float] = {}  # id_mp → total quantity needed

    for brassin in brassins:
        # 3a. Ingredients (from recipe): quantite is already the total
        #     for the full brassin volume, no need to multiply
        for ing in brassin.ingredients:
            id_mp = ing["id_mp"]
            if not id_mp:
                continue
            needed = ing["total_qty"]
            needs_by_mp[id_mp] = needs_by_mp.get(id_mp, 0) + needed

        # 3b. Packaging from matieresPremieresPlanificationConditionnement
        #     EasyBeer pre-calculates exact packaging needs — use directly
        for pkg in brassin.packaging:
            id_mp = pkg["id_mp"]
            needs_by_mp[id_mp] = needs_by_mp.get(id_mp, 0) + pkg["total_qty"]

    # ── 4. Build ComponentNeed list ──
    all_mp = get_all_matieres_premieres() or []
    mp_stock: dict[int, dict] = {}
    for mp in all_mp:
        mp_id = mp.get("idMatierePremiere")
        if mp_id:
            mp_stock[mp_id] = {
                "label": (mp.get("libelle") or "").strip(),
                "stock": float(mp.get("quantiteVirtuelle") or 0),
                "unit": (mp.get("unite") or {}).get("symbole", "u"),
                "type_code": (mp.get("type") or {}).get("code", ""),
            }

    # Build supplier map (reuse the 365-day history logic)
    supplier_map_by_id = _build_supplier_map(mp_stock)

    component_needs: list[ComponentNeed] = []
    for id_mp, total_needed in sorted(needs_by_mp.items()):
        mp_info = mp_stock.get(id_mp)
        if not mp_info:
            _log.warning("ComponentNeed: MP id=%d not found in EasyBeer", id_mp)
            continue

        stock = mp_info["stock"]
        component_needs.append(ComponentNeed(
            id_mp=id_mp,
            label=mp_info["label"],
            unit=mp_info["unit"],
            current_stock=stock,
            total_needed=total_needed,
            stock_after=stock - total_needed,
            supplier=supplier_map_by_id.get(id_mp),
            type_code=mp_info["type_code"],
        ))

    _log.info(
        "Component needs: %d MPs impacted, %d in deficit",
        len(component_needs),
        sum(1 for c in component_needs if c.stock_after < 0),
    )

    return brassins, component_needs


def _build_supplier_map(mp_stock: dict[int, dict]) -> dict[int, str]:
    """Build id_mp → fournisseur map from 365-day purchase history."""
    from common.easybeer.history import get_mp_historique_entree
    from common.easybeer._client import _dates

    supplier_map: dict[str, str] = {}  # label → fournisseur
    try:
        date_debut, date_fin = _dates(365)
        for cat in ("Conditionnement", "Ingredient", "Divers"):
            try:
                entries = get_mp_historique_entree(
                    cat, date_debut=date_debut, date_fin=date_fin,
                )
                for e in entries:
                    lib = (e.get("libelle") or "").strip()
                    four = (e.get("fournisseur") or "").strip()
                    if lib and four and lib not in supplier_map:
                        supplier_map[lib] = four
            except Exception:
                _log.warning("Erreur historique %s", cat, exc_info=True)
    except Exception:
        _log.warning("Erreur dates historique", exc_info=True)

    # Convert to id-based map
    label_to_id: dict[str, int] = {v["label"]: k for k, v in mp_stock.items()}
    supplier_map_by_id: dict[int, str] = {}
    for label, supplier in supplier_map.items():
        mp_id = label_to_id.get(label)
        if mp_id:
            supplier_map_by_id[mp_id] = supplier

    return supplier_map_by_id
