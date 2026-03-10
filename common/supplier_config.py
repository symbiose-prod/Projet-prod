"""
common/supplier_config.py
=========================
CRUD + merge for supplier ordering constraints.

Overrides stored in the ``supplier_configs`` table (JSONB) are merged on top
of the defaults from ``config.yaml`` (``stocks.supplier_groups[].ordering``).
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any

from common.data import get_stocks_config
from db.conn import run_sql

_log = logging.getLogger("ferment.supplier_config")


# ─── Helpers ────────────────────────────────────────────────────────────────

def _tenant_id() -> str:
    """Read tenant_id from the current NiceGUI session (same pattern as storage.py)."""
    try:
        from nicegui import app
        tid = app.storage.user.get("tenant_id")
        if tid:
            return str(tid)
    except Exception:
        pass
    # Fallback: use the default tenant
    from common.storage import _ensure_tenant, DEFAULT_TENANT_NAME
    return _ensure_tenant(DEFAULT_TENANT_NAME)


def _user_id() -> str | None:
    """Read current user id from session, or None."""
    try:
        from nicegui import app
        uid = app.storage.user.get("id")
        return str(uid) if uid else None
    except Exception:
        return None


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge *override* into a copy of *base* (override wins)."""
    result = copy.deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def _normalize_ordering(cfg: dict) -> dict:
    """Convert legacy field names to current names (backward compat for DB overrides).

    Renames:
        pallets          → references
        bottles_per_pallet → qty_per_unit  (inside each reference)
        min_order_pallets  → min_order
    """
    if not cfg:
        return cfg

    out = dict(cfg)

    # min_order_pallets → min_order
    if "min_order_pallets" in out and "min_order" not in out:
        out["min_order"] = out.pop("min_order_pallets")
    elif "min_order_pallets" in out:
        del out["min_order_pallets"]

    # pallets → references
    old_refs = out.pop("pallets", None)
    if old_refs and "references" not in out:
        new_refs: dict[str, dict] = {}
        for ref_name, ref_data in old_refs.items():
            new_ref = dict(ref_data)
            if "bottles_per_pallet" in new_ref and "qty_per_unit" not in new_ref:
                new_ref["qty_per_unit"] = new_ref.pop("bottles_per_pallet")
            elif "bottles_per_pallet" in new_ref:
                del new_ref["bottles_per_pallet"]
            new_refs[ref_name] = new_ref
        out["references"] = new_refs

    # Also normalize references already present (in case they use old sub-keys)
    if "references" in out and isinstance(out["references"], dict):
        for ref_data in out["references"].values():
            if isinstance(ref_data, dict):
                if "bottles_per_pallet" in ref_data and "qty_per_unit" not in ref_data:
                    ref_data["qty_per_unit"] = ref_data.pop("bottles_per_pallet")
                elif "bottles_per_pallet" in ref_data:
                    del ref_data["bottles_per_pallet"]

    return out


# ─── Read ───────────────────────────────────────────────────────────────────

def get_all_supplier_overrides(tenant_id: str | None = None) -> dict[str, dict]:
    """Return all DB overrides for a tenant: {supplier_name: config_dict}."""
    tid = tenant_id or _tenant_id()
    rows = run_sql(
        """
        SELECT supplier, config
        FROM supplier_configs
        WHERE tenant_id = :t
        """,
        {"t": tid},
    )
    out: dict[str, dict] = {}
    for r in rows or []:
        out[r["supplier"]] = r["config"] if isinstance(r["config"], dict) else {}
    return out


# ─── Write ──────────────────────────────────────────────────────────────────

def upsert_supplier_config(
    supplier: str,
    config: dict[str, Any],
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Insert or update a supplier config override (UPSERT)."""
    tid = tenant_id or _tenant_id()
    uid = user_id or _user_id()
    run_sql(
        """
        INSERT INTO supplier_configs (tenant_id, supplier, config, updated_by)
        VALUES (:t, :s, CAST(:c AS JSONB), :u)
        ON CONFLICT (tenant_id, supplier) DO UPDATE
        SET config = CAST(:c AS JSONB),
            updated_by = :u
        """,
        {"t": tid, "s": supplier, "c": json.dumps(config), "u": uid},
    )
    _log.info("Upserted supplier config for '%s' (tenant=%s)", supplier, tid)


# ─── Merge (key function) ──────────────────────────────────────────────────

def get_yaml_supplier_groups() -> list[dict[str, Any]]:
    """Return the supplier_groups list from config.yaml."""
    return get_stocks_config().get("supplier_groups", [])


def get_merged_ordering_configs(tenant_id: str | None = None) -> dict[str, dict]:
    """Merge config.yaml defaults with DB overrides for all suppliers.

    Returns ``{supplier_name: merged_ordering_dict}`` — only suppliers that
    have at least one ordering field (from yaml OR DB).
    """
    tid = tenant_id or _tenant_id()

    # 1. Base from config.yaml
    groups = get_yaml_supplier_groups()
    yaml_cfgs: dict[str, dict] = {}
    for g in groups:
        ordering = g.get("ordering")
        if ordering:
            yaml_cfgs[g["name"]] = copy.deepcopy(ordering)

    # 2. DB overrides
    db_overrides = get_all_supplier_overrides(tid)

    # 3. Merge: DB overrides yaml, field by field
    all_suppliers = set(yaml_cfgs) | set(db_overrides)
    merged: dict[str, dict] = {}
    for name in all_suppliers:
        base = yaml_cfgs.get(name, {})
        over = _normalize_ordering(db_overrides.get(name, {}))
        result = _deep_merge(base, over) if over else base
        if result:  # only include non-empty configs
            merged[name] = result

    return merged


def get_all_suppliers_with_config(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Return all suppliers from config.yaml enriched with merged ordering configs.

    Each entry: {name, icon, category, ordering: {merged_dict}}.
    Used by the Ressources page to display all suppliers.
    """
    tid = tenant_id or _tenant_id()
    groups = get_yaml_supplier_groups()
    db_overrides = get_all_supplier_overrides(tid)

    result: list[dict[str, Any]] = []
    for g in groups:
        yaml_ordering = copy.deepcopy(g.get("ordering") or {})
        db_over = _normalize_ordering(db_overrides.get(g["name"], {}))
        merged = _deep_merge(yaml_ordering, db_over) if db_over else yaml_ordering

        result.append({
            "name": g["name"],
            "icon": g.get("icon", "business"),
            "category": g.get("category", "Autre"),
            "active": g.get("active", True),
            "ordering": merged,
        })

    return result
