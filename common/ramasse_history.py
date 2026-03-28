"""
common/ramasse_history.py
=========================
Historique des ramasses envoyées — CRUD tenant-scoped.

Chaque envoi de fiche de ramasse est persisté avec ses lignes, le PDF généré,
et les métadonnées (destinataire, totaux, brassins). Permet de retrouver,
re-télécharger et renvoyer les ramasses passées.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from common._session import current_tenant_id, current_user_id
from db.conn import run_sql

_log = logging.getLogger("ferment.ramasse_history")


def save_ramasse(
    *,
    date_ramasse: date,
    destinataire: str,
    recipients: list[str],
    lines: list[dict[str, Any]],
    total_cartons: int,
    total_palettes: int,
    total_poids_kg: int,
    packaging: list[dict[str, Any]] | None = None,
    pdf_bytes: bytes | None = None,
    brassin_ids: list[str] | None = None,
    status: str = "sent",
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Persiste une ramasse envoyée. Retourne l'UUID de l'enregistrement."""
    tid = tenant_id or current_tenant_id()
    uid = user_id or current_user_id()

    rows = run_sql(
        """
        INSERT INTO ramasse_history
            (tenant_id, created_by, date_ramasse, destinataire, recipients,
             line_count, total_cartons, total_palettes, total_poids_kg,
             lines, packaging, pdf_bytes, brassin_ids, status)
        VALUES
            (:tid, :uid, :dr, :dest, :recip,
             :lc, :tc, :tp, :tpk,
             CAST(:lines AS jsonb), CAST(:pkg AS jsonb), :pdf, :bids, :st)
        RETURNING id
        """,
        {
            "tid": tid,
            "uid": uid,
            "dr": date_ramasse,
            "dest": destinataire,
            "recip": recipients,
            "lc": len(lines),
            "tc": total_cartons,
            "tp": total_palettes,
            "tpk": total_poids_kg,
            "lines": json.dumps(lines, default=str, ensure_ascii=False),
            "pkg": json.dumps(packaging or [], default=str, ensure_ascii=False),
            "pdf": pdf_bytes,
            "bids": brassin_ids or [],
            "st": status,
        },
    )
    rid = str(rows[0]["id"])
    _log.info("Ramasse sauvegardée: id=%s dest=%s cartons=%d", rid, destinataire, total_cartons)
    return rid


def list_ramasses(
    tenant_id: str | None = None,
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Liste les ramasses (sans pdf_bytes pour la perf). Triées par date desc."""
    tid = tenant_id or current_tenant_id()
    return run_sql(
        """
        SELECT id, date_ramasse, destinataire, recipients,
               line_count, total_cartons, total_palettes, total_poids_kg,
               status, created_at
        FROM ramasse_history
        WHERE tenant_id = :tid
        ORDER BY created_at DESC
        LIMIT :lim OFFSET :off
        """,
        {"tid": tid, "lim": limit, "off": offset},
    ) or []


def get_ramasse(
    ramasse_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Charge une ramasse complète (avec pdf_bytes et lignes)."""
    tid = tenant_id or current_tenant_id()
    rows = run_sql(
        """
        SELECT id, date_ramasse, destinataire, recipients,
               line_count, total_cartons, total_palettes, total_poids_kg,
               lines, packaging, pdf_bytes, brassin_ids, status, created_at
        FROM ramasse_history
        WHERE id = :rid AND tenant_id = :tid
        LIMIT 1
        """,
        {"rid": ramasse_id, "tid": tid},
    )
    return rows[0] if rows else None


def count_ramasses(tenant_id: str | None = None) -> int:
    """Nombre total de ramasses pour le tenant."""
    tid = tenant_id or current_tenant_id()
    rows = run_sql(
        "SELECT COUNT(*)::int AS n FROM ramasse_history WHERE tenant_id = :tid",
        {"tid": tid},
    )
    return int(rows[0]["n"]) if rows else 0
