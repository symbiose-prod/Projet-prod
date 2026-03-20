"""
common/product_bom.py
=====================
CRUD for product BOM (Bill of Materials) — packaging components per finished product format.

Each row maps one component (matiere premiere) to one product-format.
Example: Kéfir Gingembre 12x33 → Étiquette Kéfir Gingembre 33cl, qty_per_unit=12.
"""
from __future__ import annotations

import logging
from typing import Any

from common._session import current_tenant_id as _tenant_id
from db.conn import run_sql

_log = logging.getLogger("ferment.product_bom")


# ─── Read ───────────────────────────────────────────────────────────────────

def get_all_bom(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Return all BOM entries for a tenant, sorted by product then component."""
    tid = tenant_id or _tenant_id()
    rows = run_sql(
        """
        SELECT id, id_produit, format_code, product_label,
               id_mp, mp_label, qty_per_unit,
               validated, source, created_at, updated_at
        FROM product_bom
        WHERE tenant_id = :t
        ORDER BY product_label, format_code, mp_label
        """,
        {"t": tid},
    )
    return rows or []


def get_bom_for_product(
    id_produit: int,
    format_code: str,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return BOM entries for one product-format."""
    tid = tenant_id or _tenant_id()
    rows = run_sql(
        """
        SELECT id, id_mp, mp_label, qty_per_unit, validated, source
        FROM product_bom
        WHERE tenant_id = :t AND id_produit = :p AND format_code = :f
        ORDER BY mp_label
        """,
        {"t": tid, "p": id_produit, "f": format_code},
    )
    return rows or []


def get_bom_lookup(tenant_id: str | None = None) -> dict[int, list[dict[str, Any]]]:
    """Return BOM indexed by id_mp (component).

    ``{id_mp: [{id_produit, format_code, qty_per_unit, product_label}]}``

    This is the main lookup for the stock autonomy calculation:
    for a given component, which products use it and how much per carton.
    Only returns **validated** entries.
    """
    tid = tenant_id or _tenant_id()
    rows = run_sql(
        """
        SELECT id_mp, id_produit, format_code, qty_per_unit, product_label
        FROM product_bom
        WHERE tenant_id = :t AND validated = TRUE
        ORDER BY id_mp, product_label
        """,
        {"t": tid},
    )
    lookup: dict[int, list[dict[str, Any]]] = {}
    for r in rows or []:
        lookup.setdefault(r["id_mp"], []).append({
            "id_produit": r["id_produit"],
            "format_code": r["format_code"],
            "qty_per_unit": r["qty_per_unit"],
            "product_label": r["product_label"],
        })
    return lookup


# ─── Write ──────────────────────────────────────────────────────────────────

def upsert_bom_entry(
    id_produit: int,
    format_code: str,
    id_mp: int,
    qty_per_unit: float,
    product_label: str = "",
    mp_label: str = "",
    validated: bool = False,
    source: str = "manual",
    tenant_id: str | None = None,
) -> None:
    """Insert or update a single BOM entry (UPSERT)."""
    if qty_per_unit <= 0 or qty_per_unit > 10000:
        raise ValueError(f"qty_per_unit invalide : {qty_per_unit}")
    tid = tenant_id or _tenant_id()
    run_sql(
        """
        INSERT INTO product_bom
            (tenant_id, id_produit, format_code, product_label,
             id_mp, mp_label, qty_per_unit, validated, source)
        VALUES (:t, :p, :f, :pl, :m, :ml, :q, :v, :s)
        ON CONFLICT (tenant_id, id_produit, format_code, id_mp) DO UPDATE
        SET product_label = :pl,
            mp_label      = :ml,
            qty_per_unit  = :q,
            validated     = :v,
            source        = :s
        """,
        {
            "t": tid, "p": id_produit, "f": format_code, "pl": product_label,
            "m": id_mp, "ml": mp_label, "q": qty_per_unit,
            "v": validated, "s": source,
        },
    )
    _log.info(
        "Upserted BOM: produit=%d format=%s mp=%d qty=%.1f (tenant=%s)",
        id_produit, format_code, id_mp, qty_per_unit, tid,
    )


def bulk_upsert_bom(
    entries: list[dict[str, Any]],
    tenant_id: str | None = None,
) -> int:
    """Bulk upsert BOM entries. Returns count of entries processed.

    Each entry dict must contain: id_produit, format_code, id_mp, qty_per_unit.
    Optional: product_label, mp_label, validated, source.
    """
    tid = tenant_id or _tenant_id()
    count = 0
    for e in entries:
        run_sql(
            """
            INSERT INTO product_bom
                (tenant_id, id_produit, format_code, product_label,
                 id_mp, mp_label, qty_per_unit, validated, source)
            VALUES (:t, :p, :f, :pl, :m, :ml, :q, :v, :s)
            ON CONFLICT (tenant_id, id_produit, format_code, id_mp) DO UPDATE
            SET product_label = :pl,
                mp_label      = :ml,
                qty_per_unit  = CASE
                    WHEN product_bom.source = 'conditioning' AND :s != 'conditioning'
                    THEN product_bom.qty_per_unit
                    ELSE :q
                END,
                validated     = CASE
                    WHEN product_bom.validated = TRUE THEN TRUE
                    ELSE :v
                END,
                source        = CASE
                    WHEN product_bom.source = 'conditioning' AND :s != 'conditioning'
                    THEN product_bom.source
                    ELSE :s
                END
            """,
            {
                "t": tid,
                "p": e["id_produit"],
                "f": e["format_code"],
                "pl": e.get("product_label", ""),
                "m": e["id_mp"],
                "ml": e.get("mp_label", ""),
                "q": e.get("qty_per_unit", 0),
                "v": e.get("validated", False),
                "s": e.get("source", "auto_detected"),
            },
        )
        count += 1
    _log.info("Bulk upserted %d BOM entries (tenant=%s)", count, tid)
    return count


def delete_bom_entry(
    id_produit: int,
    format_code: str,
    id_mp: int,
    tenant_id: str | None = None,
) -> None:
    """Delete a single BOM entry."""
    tid = tenant_id or _tenant_id()
    run_sql(
        """
        DELETE FROM product_bom
        WHERE tenant_id = :t AND id_produit = :p
              AND format_code = :f AND id_mp = :m
        """,
        {"t": tid, "p": id_produit, "f": format_code, "m": id_mp},
    )
    _log.info(
        "Deleted BOM: produit=%d format=%s mp=%d (tenant=%s)",
        id_produit, format_code, id_mp, tid,
    )


def validate_all_bom(tenant_id: str | None = None) -> int:
    """Mark ALL unvalidated BOM entries as validated for a tenant.

    Returns the number of entries that were validated.
    """
    tid = tenant_id or _tenant_id()
    rows = run_sql(
        """
        UPDATE product_bom
        SET validated = TRUE
        WHERE tenant_id = :t AND validated = FALSE
        RETURNING id
        """,
        {"t": tid},
    )
    count = len(rows) if rows else 0
    _log.info("Validated ALL BOM: %d entries (tenant=%s)", count, tid)
    return count


def validate_bom(
    id_produit: int,
    format_code: str,
    tenant_id: str | None = None,
) -> int:
    """Mark all BOM entries for a product-format as validated.

    Returns the number of entries validated. Raises ValueError if no entries exist.
    Uses a single UPDATE RETURNING for atomicity.
    """
    tid = tenant_id or _tenant_id()
    rows = run_sql(
        """
        UPDATE product_bom
        SET validated = TRUE
        WHERE tenant_id = :t AND id_produit = :p AND format_code = :f
              AND validated = FALSE
        RETURNING id
        """,
        {"t": tid, "p": id_produit, "f": format_code},
    )
    count = len(rows) if rows else 0
    if count == 0:
        # Vérifier si des entrées existent (peut-être déjà toutes validées)
        existing = run_sql(
            """
            SELECT COUNT(*) AS cnt FROM product_bom
            WHERE tenant_id = :t AND id_produit = :p AND format_code = :f
            """,
            {"t": tid, "p": id_produit, "f": format_code},
        )
        total = (existing[0]["cnt"] if existing else 0)
        if total == 0:
            raise ValueError("Aucun composant à valider pour ce format.")
        # Toutes déjà validées → retourne 0 sans erreur
    _log.info(
        "Validated BOM: produit=%d format=%s (%d entries, tenant=%s)",
        id_produit, format_code, count, tid,
    )
    return count
