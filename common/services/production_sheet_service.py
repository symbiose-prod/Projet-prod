"""
common/services/production_sheet_service.py
============================================
Service métier : fiches de production digitales (mode admin beta).

Workflow :
  1. L'admin sélectionne un brassin EasyBeer en cours (ou démarre une fiche
     manuelle) → ``create_sheet()`` insère la ligne en status ``'draft'``.
  2. L'app remplit progressivement la fiche en PATCH-ant ``data`` (un champ
     ou un bloc à la fois) — ``patch_sheet()``. Auto-save à chaque champ.
  3. ``finalize_sheet()`` (Sprint 4) figera le contenu, générera un PDF et
     passera le status à ``'completed'``.

Sans NiceGUI — utilisable depuis CLI / tests.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass
from typing import Any

from db.conn import run_sql

_log = logging.getLogger("ferment.services.production_sheet")


# ─── Modèles typés ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProductionSheetSummary:
    """Résumé pour la liste (sans le contenu data / pdf_bytes lourds)."""
    id: str
    brassin_id: str | None
    produit: str
    cuve: str
    ddm: _dt.date | None
    lot: str
    status: str                  # 'draft' | 'completed'
    created_at: _dt.datetime
    updated_at: _dt.datetime
    finalized_at: _dt.datetime | None
    created_by_email: str | None


# ─── Create ─────────────────────────────────────────────────────────────────

_ALLOWED_STATUS = ("draft", "completed")


def create_sheet(
    tenant_id: str,
    *,
    user_id: str | None = None,
    brassin_id: str | None = None,
    produit: str = "",
    cuve: str = "",
    ddm: _dt.date | None = None,
    lot: str = "",
    data: dict[str, Any] | None = None,
) -> str:
    """Crée une fiche de production en status ``'draft'``.

    Tous les champs sont optionnels — l'app peut créer une fiche vide puis
    PATCH au fur et à mesure.

    Args:
        tenant_id: scope multi-tenant (UUID).
        user_id: créateur (UUID). Peut être ``None`` (service interne).
        brassin_id: ID du brassin EasyBeer (si lié), ``None`` pour fiche
            manuelle.
        produit: libellé produit (ex: "K. Mangue - Passion").
        cuve: ex "Cuve de 7200L".
        ddm: date de durabilité minimale.
        lot: format DDMMYYYY si dérivé de DDM, sinon libre.
        data: contenu initial du formulaire (sections JSON). Si ``None``,
            ``{}`` par défaut.

    Returns:
        UUID de la fiche créée.
    """
    rows = run_sql(
        """
        INSERT INTO production_sheets
            (tenant_id, created_by, brassin_id, produit, cuve, ddm, lot,
             status, data)
        VALUES
            (:tid, :uid, :bid, :prod, :cuve, :ddm, :lot, 'draft',
             CAST(:data AS jsonb))
        RETURNING id
        """,
        {
            "tid": tenant_id,
            "uid": user_id,
            "bid": brassin_id,
            "prod": produit or "",
            "cuve": cuve or "",
            "ddm": ddm,
            "lot": lot or "",
            "data": json.dumps(data or {}, default=str, ensure_ascii=False),
        },
    )
    sid = str(rows[0]["id"])
    _log.info(
        "Fiche production créée : id=%s brassin=%s produit=%s tenant=%s user=%s",
        sid, brassin_id, produit, tenant_id, user_id,
    )
    return sid


# ─── List ───────────────────────────────────────────────────────────────────

def list_sheets(
    tenant_id: str,
    *,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
) -> list[ProductionSheetSummary]:
    """Liste les fiches du tenant, triées par created_at desc.

    Args:
        status: filtre optionnel ``'draft'`` | ``'completed'``.
    """
    where_status = ""
    params: dict[str, Any] = {
        "tid": tenant_id, "lim": int(limit), "off": int(offset),
    }
    if status in _ALLOWED_STATUS:
        where_status = " AND ps.status = :status"
        params["status"] = status

    rows = run_sql(
        f"""
        SELECT ps.id, ps.brassin_id, ps.produit, ps.cuve, ps.ddm, ps.lot,
               ps.status, ps.created_at, ps.updated_at, ps.finalized_at,
               u.email AS created_by_email
          FROM production_sheets ps
          LEFT JOIN users u ON u.id = ps.created_by
         WHERE ps.tenant_id = :tid{where_status}
         ORDER BY ps.created_at DESC
         LIMIT :lim OFFSET :off
        """,
        params,
    ) or []

    out: list[ProductionSheetSummary] = []
    for r in rows:
        ddm = r.get("ddm")
        ddm_date = (
            ddm if isinstance(ddm, _dt.date) or ddm is None
            else _dt.date.fromisoformat(str(ddm)[:10])
        )
        out.append(ProductionSheetSummary(
            id=str(r["id"]),
            brassin_id=r.get("brassin_id"),
            produit=str(r.get("produit") or ""),
            cuve=str(r.get("cuve") or ""),
            ddm=ddm_date,
            lot=str(r.get("lot") or ""),
            status=str(r.get("status") or "draft"),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            finalized_at=r.get("finalized_at"),
            created_by_email=r.get("created_by_email"),
        ))
    return out


def count_sheets(
    tenant_id: str,
    *,
    status: str | None = None,
) -> int:
    """Nombre total de fiches pour pagination."""
    where_status = ""
    params: dict[str, Any] = {"tid": tenant_id}
    if status in _ALLOWED_STATUS:
        where_status = " AND status = :status"
        params["status"] = status
    rows = run_sql(
        f"SELECT COUNT(*) AS n FROM production_sheets "
        f"WHERE tenant_id = :tid{where_status}",
        params,
    )
    return int(rows[0]["n"]) if rows else 0


# ─── Conditionnement réel : agrégation depuis SSCC (Sprint 2 pre-fill) ─────

@dataclass(frozen=True)
class ConditionnementLine:
    """Une ligne agrégée du conditionnement réel — par (format, marque)."""
    fmt: str                       # ex: "12x33", "6x75"
    marque: str                    # ex: "SYMBIOSE", "NIKO"
    designation: str               # libellé produit (premier rencontré)
    cartons: int                   # SUM(sscc_log.case_count)
    palettes: int                  # COUNT(*) palettes distinctes


@dataclass(frozen=True)
class ConditionnementByLot:
    """Conteneur d'agrégation conditionnement réel pour un lot donné."""
    lot: str
    items: list[ConditionnementLine]
    total_cartons: int
    total_palettes: int


def compute_real_conditionnement_by_lot(
    tenant_id: str,
    lot: str,
) -> ConditionnementByLot:
    """Agrège les palettes étiquetées pour un lot donné en lignes (fmt, marque).

    Source de vérité : ``sscc_log`` (palettes générées) JOIN
    ``etiquette_palette_history`` (infos produit) — filtre par tenant + lot
    + ``voided_at IS NULL`` (palettes annulées exclues).

    Pré-remplit la section "Conditionnement réel" de la fiche papier :
    cartons + palettes par (format × marque) calculés à partir des scans SSCC
    déjà réalisés. Idempotent et toujours à jour : on peut appeler à tout
    moment pendant le conditionnement pour rafraîchir.

    Si ``lot`` est vide, retourne un résultat vide (jamais d'agrégation
    par tenant sans lot — protection accidentelle).

    Args:
        tenant_id: scope multi-tenant.
        lot: ex "15052027" (format DDMMYYYY) ou tout autre format de lot.

    Returns:
        ``ConditionnementByLot`` avec ``items`` triés par (fmt, marque) et
        ``total_cartons`` / ``total_palettes`` agrégés sur tout le lot.
    """
    if not lot or not lot.strip():
        return ConditionnementByLot(lot="", items=[], total_cartons=0, total_palettes=0)

    rows = run_sql(
        """
        SELECT eph.fmt, eph.marque, eph.designation,
               COALESCE(SUM(sl.case_count), 0) AS total_cartons,
               COUNT(*) AS total_palettes
          FROM sscc_log sl
          JOIN etiquette_palette_history eph
                ON eph.sscc = sl.sscc AND eph.tenant_id = sl.tenant_id
         WHERE sl.tenant_id = :tid
           AND sl.lot = :lot
           AND sl.voided_at IS NULL
         GROUP BY eph.fmt, eph.marque, eph.designation
         ORDER BY eph.fmt, eph.marque
        """,
        {"tid": tenant_id, "lot": lot.strip()},
    ) or []

    items = [
        ConditionnementLine(
            fmt=str(r.get("fmt") or ""),
            marque=str(r.get("marque") or ""),
            designation=str(r.get("designation") or ""),
            cartons=int(r.get("total_cartons") or 0),
            palettes=int(r.get("total_palettes") or 0),
        )
        for r in rows
    ]
    total_c = sum(i.cartons for i in items)
    total_p = sum(i.palettes for i in items)
    return ConditionnementByLot(
        lot=lot.strip(),
        items=items,
        total_cartons=total_c,
        total_palettes=total_p,
    )
