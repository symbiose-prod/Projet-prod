"""
common/sync — Synchronisation étiquettes SaaS → Base Access.

Expose les fonctions principales :
  - collect_label_data()      : collecte EasyBeer → liste produits
  - create_sync_operation()   : enregistre une opération REPLACE_ALL
"""
from __future__ import annotations

import json
import logging
from typing import Any

from db.conn import run_sql

_log = logging.getLogger("ferment.sync")


def create_sync_operation(
    products: list[dict[str, Any]],
    tenant_id: str,
    triggered_by: str = "scheduler",
) -> dict[str, Any]:
    """Crée une opération REPLACE_ALL dans sync_operations (status=pending).

    Retourne {"id": <op_id>, "product_count": N}.
    """
    payload_json = json.dumps(products, ensure_ascii=False, default=str)
    rows = run_sql(
        """INSERT INTO sync_operations (tenant_id, op_type, payload, product_count, triggered_by)
           VALUES (:t, 'REPLACE_ALL', :p::jsonb, :n, :tb)
           RETURNING id, product_count, created_at""",
        {"t": tenant_id, "p": payload_json, "n": len(products), "tb": triggered_by},
    )
    row = rows[0]
    _log.info(
        "Sync operation #%s created: %d products (triggered_by=%s)",
        row["id"], row["product_count"], triggered_by,
    )
    return {"id": row["id"], "product_count": row["product_count"]}
