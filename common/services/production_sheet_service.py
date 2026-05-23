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


# ─── Get détail ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProductionSheetDetail:
    """Fiche complète (avec ``data`` JSONB) pour l'éditeur mobile."""
    id: str
    brassin_id: str | None
    produit: str
    cuve: str
    ddm: _dt.date | None
    lot: str
    status: str
    data: dict[str, Any]
    created_at: _dt.datetime
    updated_at: _dt.datetime
    finalized_at: _dt.datetime | None
    created_by_email: str | None
    # Compteur optimistic-lock — incrémenté à chaque PATCH réussi.
    version: int = 1


def get_sheet(
    tenant_id: str,
    sheet_id: str,
) -> ProductionSheetDetail | None:
    """Charge une fiche complète (avec son contenu ``data``).

    Retourne ``None`` si introuvable ou hors tenant.
    """
    rows = run_sql(
        """
        SELECT ps.id, ps.brassin_id, ps.produit, ps.cuve, ps.ddm, ps.lot,
               ps.status, ps.version, ps.data, ps.created_at, ps.updated_at,
               ps.finalized_at,
               u.email AS created_by_email
          FROM production_sheets ps
          LEFT JOIN users u ON u.id = ps.created_by
         WHERE ps.id = :sid AND ps.tenant_id = :tid
         LIMIT 1
        """,
        {"sid": sheet_id, "tid": tenant_id},
    )
    if not rows:
        return None
    r = rows[0]
    ddm = r.get("ddm")
    ddm_date = (
        ddm if isinstance(ddm, _dt.date) or ddm is None
        else _dt.date.fromisoformat(str(ddm)[:10])
    )
    raw_data = r.get("data") or {}
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except (ValueError, TypeError):
            raw_data = {}
    if not isinstance(raw_data, dict):
        raw_data = {}
    return ProductionSheetDetail(
        id=str(r["id"]),
        brassin_id=r.get("brassin_id"),
        produit=str(r.get("produit") or ""),
        cuve=str(r.get("cuve") or ""),
        ddm=ddm_date,
        lot=str(r.get("lot") or ""),
        status=str(r.get("status") or "draft"),
        data=raw_data,
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        finalized_at=r.get("finalized_at"),
        created_by_email=r.get("created_by_email"),
        version=int(r.get("version") or 1),
    )


# ─── PATCH partiel (auto-save iOS) ──────────────────────────────────────────

# Sentinelle pour distinguer "champ pas fourni" (= ne pas toucher) de "fourni
# avec valeur None" (= mettre NULL/vide). Pythonique idiomatique.
_UNSET = object()


def patch_sheet(
    tenant_id: str,
    sheet_id: str,
    *,
    produit: str | None | object = _UNSET,
    cuve: str | None | object = _UNSET,
    ddm: _dt.date | None | object = _UNSET,
    lot: str | None | object = _UNSET,
    brassin_id: str | None | object = _UNSET,
    data: dict[str, Any] | None | object = _UNSET,
    expected_version: int | None = None,
) -> str:
    """Mise à jour partielle d'une fiche en status ``'draft'``.

    Sémantique : on n'écrase que les champs fournis. Le sentinel ``_UNSET``
    distingue "non fourni" (pas dans le SET SQL) de "fourni avec None" (NULL/
    vide). Côté client mobile, l'auto-save envoie typiquement 1-2 champs à
    la fois (PATCH minimal).

    Pour ``data`` : on remplace le JSONB entier. Le client iOS garde la
    version courante en mémoire, modifie le champ puis renvoie tout. Pas de
    merge profond côté serveur (simple, OK car 1 seul opérateur édite à la
    fois en mode admin beta).

    Bloque les modifications sur les fiches finalisées (``status='completed'``).

    Optimistic locking : ``version`` est incrémenté à chaque PATCH réussi.
    Si ``expected_version`` est fourni, l'UPDATE n'aboutit que si la version
    en base correspond — sinon un autre client a édité entre temps (conflit).
    Si ``expected_version`` est ``None``, comportement legacy last-write-wins
    (clients iOS pas encore mis à jour).

    Returns:
        ``"ok"``         : l'UPDATE a affecté la fiche.
        ``"no_changes"`` : aucun champ à patcher.
        ``"not_found"``  : fiche introuvable, hors tenant, ou déjà finalisée.
        ``"conflict"``   : ``expected_version`` périmée — un autre client a
                           modifié la fiche depuis sa dernière lecture.
    """
    updates: list[str] = []
    params: dict[str, Any] = {"sid": sheet_id, "tid": tenant_id}

    if produit is not _UNSET:
        updates.append("produit = :produit")
        params["produit"] = str(produit or "")
    if cuve is not _UNSET:
        updates.append("cuve = :cuve")
        params["cuve"] = str(cuve or "")
    if lot is not _UNSET:
        updates.append("lot = :lot")
        params["lot"] = str(lot or "")
    if brassin_id is not _UNSET:
        updates.append("brassin_id = :bid")
        params["bid"] = brassin_id or None
    if ddm is not _UNSET:
        updates.append("ddm = :ddm")
        params["ddm"] = ddm
    if data is not _UNSET:
        updates.append("data = CAST(:data AS jsonb)")
        params["data"] = json.dumps(data or {}, default=str, ensure_ascii=False)

    if not updates:
        return "no_changes"

    # Le compteur s'incrémente à chaque PATCH réussi, qu'un expected_version
    # ait été fourni ou non — pour que les clients qui lisent la version
    # restent cohérents.
    updates.append("version = version + 1")

    where_version = ""
    if expected_version is not None:
        where_version = " AND version = :expver"
        params["expver"] = int(expected_version)

    sql = f"""
        UPDATE production_sheets
           SET {', '.join(updates)}
         WHERE id = :sid
           AND tenant_id = :tid
           AND status = 'draft'{where_version}
        RETURNING id
    """
    rows = run_sql(sql, params)
    if rows:
        _log.info(
            "Fiche production patch : id=%s tenant=%s fields=%s",
            sheet_id, tenant_id,
            [u.split(" = ")[0] for u in updates if not u.startswith("version")],
        )
        return "ok"

    # 0 ligne affectée — distinguer fiche absente/finalisée d'un conflit de
    # version. Un SELECT simple : si la fiche existe et est en 'draft', c'est
    # nécessairement expected_version qui était périmée.
    check = run_sql(
        "SELECT status FROM production_sheets "
        "WHERE id = :sid AND tenant_id = :tid LIMIT 1",
        {"sid": sheet_id, "tid": tenant_id},
    )
    if not check or str(check[0].get("status")) != "draft":
        return "not_found"
    _log.info(
        "Fiche production patch : conflit de version id=%s tenant=%s "
        "expected=%s", sheet_id, tenant_id, expected_version,
    )
    return "conflict"


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


def find_sheets_by_brassin_ids(
    tenant_id: str,
    brassin_ids: list[str],
) -> dict[str, ProductionSheetSummary]:
    """Pour chaque brassin_id fourni, retourne la fiche ``draft`` la plus
    récente associée (s'il en existe une). Format : ``{brassin_id: Summary}``.

    Sert à enrichir la liste des brassins EB côté mobile : chaque brassin
    sait s'il a déjà une fiche démarrée ou non. Les brassins sans fiche
    ne sont pas inclus dans le dict (le caller fait ``dict.get(bid)``).

    Une seule requête SQL avec ``DISTINCT ON (brassin_id)`` + tri par
    ``created_at DESC`` → on récupère la fiche la plus récente par brassin.

    Note implémentation : on ne filtre pas par `brassin_id IN (...)` dans
    le SQL (psycopg/SQLAlchemy params nommés rendent les IN complexes).
    À la place on charge tous les drafts du tenant et on filtre côté Python
    avec un set. Pour < 100 drafts simultanés c'est négligeable, et le
    nombre de drafts actifs est en pratique très limité (1-5).
    """
    if not brassin_ids:
        return {}
    requested = set(brassin_ids)
    rows = run_sql(
        """
        SELECT DISTINCT ON (ps.brassin_id)
               ps.id, ps.brassin_id, ps.produit, ps.cuve, ps.ddm, ps.lot,
               ps.status, ps.created_at, ps.updated_at, ps.finalized_at,
               u.email AS created_by_email
          FROM production_sheets ps
          LEFT JOIN users u ON u.id = ps.created_by
         WHERE ps.tenant_id = :tid
           AND ps.brassin_id IS NOT NULL
           AND ps.status = 'draft'
         ORDER BY ps.brassin_id, ps.created_at DESC
        """,
        {"tid": tenant_id},
    ) or []
    out: dict[str, ProductionSheetSummary] = {}
    for r in rows:
        bid = r.get("brassin_id")
        if not bid or bid not in requested:
            continue
        ddm = r.get("ddm")
        ddm_date = (
            ddm if isinstance(ddm, _dt.date) or ddm is None
            else _dt.date.fromisoformat(str(ddm)[:10])
        )
        out[str(bid)] = ProductionSheetSummary(
            id=str(r["id"]),
            brassin_id=str(bid),
            produit=str(r.get("produit") or ""),
            cuve=str(r.get("cuve") or ""),
            ddm=ddm_date,
            lot=str(r.get("lot") or ""),
            status=str(r.get("status") or "draft"),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            finalized_at=r.get("finalized_at"),
            created_by_email=r.get("created_by_email"),
        )
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


# ─── Finalisation (Sprint 4) ────────────────────────────────────────────────

def finalize_sheet(
    tenant_id: str,
    sheet_id: str,
    *,
    user_email: str = "",
) -> tuple[ProductionSheetDetail, bytes]:
    """Finalise une fiche en status ``'draft'`` → ``'completed'``.

    1. Charge la fiche complète.
    2. Vérifie qu'elle est en ``'draft'`` (sinon ValueError).
    3. Génère le PDF via ``common.production_sheet_pdf.build_production_sheet_pdf``.
    4. UPDATE atomique : status='completed' + finalized_at=now() + pdf_bytes.
    5. Retourne la fiche post-update + les bytes du PDF (pour download immédiat).

    La fiche reste accessible en lecture après finalisation mais ne peut plus
    être éditée (le PATCH refuse si status != 'draft').

    Raises:
        ValueError: fiche introuvable / hors tenant / déjà finalisée.
    """
    from common.production_sheet_pdf import build_production_sheet_pdf

    current = get_sheet(tenant_id, sheet_id)
    if current is None:
        raise ValueError("Sheet not found")
    if current.status != "draft":
        raise ValueError(
            f"Sheet already finalized (status='{current.status}')",
        )

    pdf_bytes = build_production_sheet_pdf(current)

    rows = run_sql(
        """
        UPDATE production_sheets
           SET status       = 'completed',
               finalized_at = now(),
               pdf_bytes    = :pdf
         WHERE id        = :sid
           AND tenant_id = :tid
           AND status    = 'draft'
        RETURNING id
        """,
        {"sid": sheet_id, "tid": tenant_id, "pdf": pdf_bytes},
    )
    if not rows:
        # Race condition : un autre process a finalisé entre temps
        raise ValueError("Sheet not found or not in draft status")

    _log.info(
        "Fiche production finalisée : id=%s tenant=%s user=%s pdf_size=%d",
        sheet_id, tenant_id, user_email, len(pdf_bytes),
    )

    # Recharge pour avoir les timestamps mis à jour
    updated = get_sheet(tenant_id, sheet_id)
    if updated is None:
        # Très improbable (on vient d'UPDATE qui a retourné une ligne)
        raise ValueError("Sheet disappeared after finalize")

    # ─── Branchement Easybeer (Sprint 2 bis) ──────────────────────────
    # Pousse les events EB correspondants via l'outbox (best-effort, ne
    # fait jamais échouer la finalize locale). Désactivé par défaut, à
    # activer via env var EB_OUTBOX_BIND_PRODUCTION_SHEETS=true.
    try:
        from common.services.production_sheet_eb_bind import (
            enqueue_eb_events_from_sheet,
        )
        bind_summary = enqueue_eb_events_from_sheet(
            updated, tenant_id=tenant_id, user_email=user_email,
        )
        if bind_summary.get("enqueued"):
            _log.info(
                "EB bind finalize : sheet=%s enqueued=%s skipped=%s errors=%s",
                sheet_id,
                bind_summary["enqueued"],
                bind_summary["skipped"],
                bind_summary["errors"],
            )
    except Exception:
        # Ne jamais faire échouer la finalize locale pour un problème EB
        _log.exception(
            "EB bind finalize failed (non-fatal) for sheet %s", sheet_id,
        )

    return (updated, pdf_bytes)


def get_sheet_pdf(
    tenant_id: str,
    sheet_id: str,
) -> bytes | None:
    """Retourne le PDF stocké d'une fiche finalisée. ``None`` si pas de PDF.

    Utilisé pour re-télécharger une fiche déjà finalisée (sans la re-générer).
    """
    rows = run_sql(
        """
        SELECT pdf_bytes
          FROM production_sheets
         WHERE id = :sid AND tenant_id = :tid
        """,
        {"sid": sheet_id, "tid": tenant_id},
    )
    if not rows:
        return None
    pdf = rows[0].get("pdf_bytes")
    return bytes(pdf) if pdf else None


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
