"""
common/bom_detection.py
=======================
Auto-detection of BOM entries from EasyBeer data.

Parses the barcode matrix to discover product formats, then matches
each format to its packaging components (bottles, labels, caps, cartons)
from the matières premières list using heuristics.

Detected entries are stored with ``validated=False`` so the user can
review and confirm them on the /nomenclatures page.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

_log = logging.getLogger("ferment.bom_detection")


# ─── Text normalization ────────────────────────────────────────────────────

def _strip_accents(s: str) -> str:
    """Remove accents: 'Kéfir Pêche' → 'Kefir Peche'."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(ch)
    )


def _normalize(s: str) -> str:
    """Lowercase + strip accents + collapse whitespace."""
    return re.sub(r"\s+", " ", _strip_accents(s).lower()).strip()


# ─── Product format detection from barcode matrix ──────────────────────────

def detect_product_formats(
    barcode_matrix: dict[str, Any],
    products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Parse barcode matrix → list of product-formats.

    Returns::

        [
            {
                "id_produit": 42,
                "libelle": "Kéfir Gingembre",
                "formats": [
                    {"format_code": "12x33", "contenance": 0.33, "lot_qty": 12},
                    {"format_code": "6x75", "contenance": 0.75, "lot_qty": 6},
                ]
            },
            ...
        ]
    """
    # Build idProduit → libelle lookup from products list
    id_to_label: dict[int, str] = {}
    for p in products:
        pid = p.get("idProduit")
        lib = (p.get("libelle") or "").strip()
        if pid and lib:
            id_to_label[pid] = lib

    # Parse barcode matrix
    product_formats: dict[int, dict[str, dict]] = {}  # id → {format_code: info}

    for prod_entry in barcode_matrix.get("produits", []):
        for cb in prod_entry.get("codesBarres", []):
            mod_produit = cb.get("modeleProduit") or {}
            id_produit = mod_produit.get("idProduit")
            if not id_produit:
                continue

            mod_cont = cb.get("modeleContenant") or {}
            contenance = round(float(mod_cont.get("contenance") or 0), 2)

            mod_lot = cb.get("modeleLot") or {}
            lot_libelle = (mod_lot.get("libelle") or "").strip()

            if not contenance:
                continue

            # Derive format code
            vol_cl = int(contenance * 100)
            m_pkg = re.search(r"(\d+)", lot_libelle)
            lot_qty = int(m_pkg.group(1)) if m_pkg else 0
            if not (vol_cl and lot_qty):
                continue

            format_code = f"{lot_qty}x{vol_cl}"

            if id_produit not in product_formats:
                product_formats[id_produit] = {}
            if format_code not in product_formats[id_produit]:
                product_formats[id_produit][format_code] = {
                    "format_code": format_code,
                    "contenance": contenance,
                    "lot_qty": lot_qty,
                }

    # Build result list
    result: list[dict[str, Any]] = []
    for pid, formats in sorted(product_formats.items()):
        libelle = id_to_label.get(pid, f"Produit #{pid}")
        result.append({
            "id_produit": pid,
            "libelle": libelle,
            "formats": sorted(formats.values(), key=lambda f: f["format_code"]),
        })

    result.sort(key=lambda r: r["libelle"])
    return result


# ─── BOM detection for one product-format ──────────────────────────────────

def _extract_flavor(product_label: str) -> str:
    """Extract the flavor/variant portion of a product name.

    'Kéfir de fruits Menthe Citron vert' → 'menthe citron vert'
    'Infusion de Kéfir Pêche Verveine'  → 'peche verveine'

    Strips common prefixes like 'Kéfir', 'Kéfir de fruits', 'Infusion de Kéfir'.
    """
    s = product_label.strip()
    # Remove common prefixes (longest first)
    for prefix in [
        "Infusion de Kéfir de fruits",
        "Infusion de Kéfir",
        "Kéfir de fruits",
        "Kéfir d'eau",
        "Kéfir",
    ]:
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].strip()
            # Remove leading "de " or "d'" if present
            s = re.sub(r"^(?:de\s+|d')", "", s, flags=re.IGNORECASE).strip()
            break
    return _normalize(s)


def detect_bom_for_format(
    id_produit: int,
    product_label: str,
    format_code: str,
    contenance: float,
    lot_qty: int,
    all_mp: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect packaging components for one product-format.

    Returns list of BOM entry dicts ready for ``bulk_upsert_bom()``.
    """
    entries: list[dict[str, Any]] = []
    flavor = _extract_flavor(product_label)
    vol_cl = int(contenance * 100)

    for mp in all_mp:
        if not mp.get("actif", True):
            continue

        mp_id = mp.get("idMatierePremiere")
        mp_label = (mp.get("libelle") or "").strip()
        mp_type = (mp.get("type") or {}).get("code", "")
        mp_norm = _normalize(mp_label)

        if not mp_id or not mp_label:
            continue

        matched_qty: float | None = None

        # ── Bottles: match CONTENANT by volume ──
        if mp_type == "CONTENANT":
            # Check if the MP label mentions the right volume
            if (
                f"{contenance}" in mp_label
                or f"{vol_cl}cl" in mp_label.lower()
                or f"0.{vol_cl}" in mp_label
            ):
                # Exclude "SAFT" bottles unless product mentions SAFT
                mp_is_saft = "saft" in mp_norm
                product_is_saft = "saft" in _normalize(product_label)
                if mp_is_saft == product_is_saft:
                    matched_qty = lot_qty  # one bottle per unit in carton

        # ── Labels: match CONDITIONNEMENT by flavor name ──
        elif mp_type == "CONDITIONNEMENT":
            is_etiquette = "etiquet" in mp_norm or "étiq" in mp_label.lower()
            is_capsule = "capsul" in mp_norm or "bouchon" in mp_norm
            is_carton = "carton" in mp_norm

            if is_etiquette and flavor:
                # Check if the label MP contains the flavor AND volume
                if flavor in mp_norm and str(vol_cl) in mp_norm:
                    matched_qty = lot_qty  # one label per bottle

            elif is_capsule:
                # Capsules: match by volume if specified, or generic
                if str(vol_cl) in mp_norm or not re.search(r"\d+cl", mp_norm):
                    matched_qty = lot_qty  # one cap per bottle

            elif is_carton:
                # Cartons: match by format (e.g., "12x33" or "12×33")
                carton_match = re.search(r"(\d+)\s*[x×]\s*(\d+)", mp_norm)
                if carton_match:
                    c_qty = int(carton_match.group(1))
                    c_vol = int(carton_match.group(2))
                    if c_qty == lot_qty and c_vol == vol_cl:
                        matched_qty = 1  # one carton per sales unit

        if matched_qty is not None:
            entries.append({
                "id_produit": id_produit,
                "format_code": format_code,
                "product_label": product_label,
                "id_mp": mp_id,
                "mp_label": mp_label,
                "qty_per_unit": matched_qty,
                "validated": False,
                "source": "auto_detected",
            })
            _log.info(
                "Auto-detected: %s %s → %s (qty=%.0f)",
                product_label, format_code, mp_label, matched_qty,
            )

    return entries


# ─── Full detection orchestrator ───────────────────────────────────────────

def run_full_detection(tenant_id: str | None = None) -> tuple[int, int]:
    """Run full BOM auto-detection from EasyBeer data.

    1. Fetch barcode matrix + products + matières premières
    2. Detect product formats
    3. For each format, detect packaging components
    4. Bulk upsert into DB (without overwriting validated or conditioning entries)

    Returns ``(total_detected, products_detected)``.
    """
    from common.easybeer.conditioning import get_code_barre_matrice
    from common.easybeer.products import get_all_products
    from common.easybeer.stocks import get_all_matieres_premieres
    from common.product_bom import bulk_upsert_bom

    _log.info("Starting full BOM detection...")

    # Fetch data from EasyBeer
    barcode_matrix = get_code_barre_matrice()
    products = get_all_products()
    all_mp = get_all_matieres_premieres()

    # Detect product formats
    product_formats = detect_product_formats(barcode_matrix, products)
    _log.info("Detected %d products with formats", len(product_formats))

    # Detect BOM for each product-format
    all_entries: list[dict[str, Any]] = []
    for pf in product_formats:
        for fmt in pf["formats"]:
            entries = detect_bom_for_format(
                id_produit=pf["id_produit"],
                product_label=pf["libelle"],
                format_code=fmt["format_code"],
                contenance=fmt["contenance"],
                lot_qty=fmt["lot_qty"],
                all_mp=all_mp,
            )
            all_entries.extend(entries)

    # Bulk upsert (respects existing validated/conditioning entries)
    if all_entries:
        bulk_upsert_bom(all_entries, tenant_id=tenant_id)

    _log.info(
        "BOM detection complete: %d entries for %d products",
        len(all_entries), len(product_formats),
    )
    return len(all_entries), len(product_formats)
