"""
common/services/print_jobs_service.py
=====================================
Service domaine : queue d'impression Brother QL-1110NWBc via agent local.

Flow :
  1. L'opérateur tape "Imprimer directement" sur la page étiquette palette.
  2. ``create_print_job`` insère un job (status=pending) avec le PDF en BYTEA.
  3. L'agent Python sur Windows long-poll ``GET /api/print-jobs/next``.
  4. ``take_next_pending_job`` réserve atomiquement le job (status=printing).
  5. Une fois imprimé, l'agent confirme via ``mark_job_printed``. En cas
     d'erreur (driver, papier, etc.), ``mark_job_error``.

L'auth de l'agent est gérée côté API via le bearer token ``PRINT_AGENT_TOKEN``
en env var (partagé entre le VPS et l'agent). Mono-tenant pour l'instant ;
le ``tenant_id`` est résolu depuis l'env var côté VPS.

Ce module est sans NiceGUI : utilisable depuis CLI / cron / tests.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass

from db.conn import run_sql

_log = logging.getLogger("ferment.services.print_jobs")


@dataclass(frozen=True)
class PrintJob:
    """Un job d'impression prêt pour l'agent Windows."""
    id: int
    pdf_bytes: bytes
    filename: str
    n_copies: int
    created_at: _dt.datetime


@dataclass(frozen=True)
class PendingJobView:
    """Vue allégée d'un job en attente — sans les bytes PDF, pour l'UI."""
    id: int
    filename: str
    n_copies: int
    status: str             # 'pending' | 'printing'
    created_at: _dt.datetime


def create_print_job(
    tenant_id: str,
    *,
    user_email: str,
    pdf_bytes: bytes,
    filename: str,
    n_copies: int = 1,
) -> int:
    """Crée un job d'impression en attente. Retourne l'id BIGSERIAL.

    Le PDF est stocké en BYTEA dans la table — pas de système de fichiers.
    Pour 102×164 mm, ~50 KB par PDF, donc largement gérable côté DB.
    """
    rows = run_sql(
        """INSERT INTO print_jobs
           (tenant_id, user_email, pdf_bytes, filename, n_copies, status)
           VALUES (:t, :u, :pdf, :fn, :n, 'pending')
           RETURNING id""",
        {
            "t": tenant_id,
            "u": user_email or "",
            "pdf": pdf_bytes,
            "fn": filename or "etiquette.pdf",
            "n": int(n_copies),
        },
    )
    return int(rows[0]["id"]) if rows else 0


def take_next_pending_job(tenant_id: str) -> PrintJob | None:
    """Réserve atomiquement le prochain job en attente pour ce tenant.

    Utilise ``FOR UPDATE SKIP LOCKED`` pour éviter qu'un second agent
    (cas paranoïaque) prenne le même job. Marque comme 'printing' avec
    ``taken_at = now()`` avant de retourner.

    Retourne ``None`` si aucun job en attente.
    """
    # On fait un CTE pour combiner SELECT + UPDATE atomiquement.
    # FOR UPDATE SKIP LOCKED = standard PostgreSQL pour les job queues.
    rows = run_sql(
        """WITH next_job AS (
              SELECT id FROM print_jobs
              WHERE tenant_id = :t AND status = 'pending'
              ORDER BY created_at
              LIMIT 1
              FOR UPDATE SKIP LOCKED
           )
           UPDATE print_jobs SET
              status = 'printing',
              taken_at = now()
           WHERE id IN (SELECT id FROM next_job)
           RETURNING id, pdf_bytes, filename, n_copies, created_at""",
        {"t": tenant_id},
    )
    if not rows:
        return None
    r = rows[0]
    return PrintJob(
        id=int(r["id"]),
        pdf_bytes=bytes(r["pdf_bytes"]),
        filename=str(r["filename"] or "etiquette.pdf"),
        n_copies=int(r["n_copies"] or 1),
        created_at=r["created_at"],
    )


def mark_job_printed(tenant_id: str, job_id: int) -> bool:
    """Marque un job comme imprimé. Retourne True si succès, False sinon."""
    rows = run_sql(
        """UPDATE print_jobs SET
              status = 'printed',
              printed_at = now()
           WHERE id = :id AND tenant_id = :t AND status = 'printing'
           RETURNING id""",
        {"id": int(job_id), "t": tenant_id},
    )
    return bool(rows)


def mark_job_error(tenant_id: str, job_id: int, error: str) -> bool:
    """Marque un job comme en erreur (avec message). Retourne True si succès."""
    rows = run_sql(
        """UPDATE print_jobs SET
              status = 'error',
              error_message = :err,
              printed_at = now()
           WHERE id = :id AND tenant_id = :t AND status IN ('printing', 'pending')
           RETURNING id""",
        {"id": int(job_id), "t": tenant_id, "err": (error or "")[:500]},
    )
    return bool(rows)


def list_pending_jobs(tenant_id: str, limit: int = 10) -> list[PendingJobView]:
    """Liste les jobs encore non imprimés (pending + printing) pour ce tenant.

    Utilisé par l'UI pour afficher dans la sidebar « À imprimer ».
    Exclut le PDF binaire pour rester rapide (~5 KB par appel).
    """
    try:
        rows = run_sql(
            """SELECT id, filename, n_copies, status, created_at
               FROM print_jobs
               WHERE tenant_id = :t AND status IN ('pending', 'printing')
               ORDER BY created_at
               LIMIT :n""",
            {"t": tenant_id, "n": int(limit)},
        ) or []
    except Exception:
        _log.exception("Échec list_pending_jobs")
        return []
    out: list[PendingJobView] = []
    for r in rows:
        try:
            out.append(PendingJobView(
                id=int(r["id"]),
                filename=str(r["filename"] or ""),
                n_copies=int(r["n_copies"] or 1),
                status=str(r["status"] or "pending"),
                created_at=r["created_at"],
            ))
        except (KeyError, TypeError, ValueError):
            _log.warning("Ligne print_jobs invalide ignorée : %r", r, exc_info=True)
    return out


def reset_stuck_jobs(tenant_id: str, stuck_after_minutes: int = 5) -> int:
    """Watchdog : remet en 'pending' les jobs bloqués en 'printing' depuis
    plus de N minutes (cas où l'agent crash en plein print).

    Retourne le nombre de jobs remis en pending.
    """
    rows = run_sql(
        """UPDATE print_jobs SET
              status = 'pending',
              taken_at = NULL
           WHERE tenant_id = :t
             AND status = 'printing'
             AND taken_at < now() - (:m::text || ' minutes')::interval
           RETURNING id""",
        {"t": tenant_id, "m": int(stuck_after_minutes)},
    )
    n = len(rows or [])
    if n > 0:
        _log.warning(
            "Reset %d jobs bloqués en 'printing' > %d min pour tenant %s",
            n, stuck_after_minutes, tenant_id,
        )
    return n


def purge_old_jobs(tenant_id: str, keep_days: int = 7) -> int:
    """Purge les jobs printed/error de plus de N jours.

    BYTEA peut prendre de la place — on garde 7 jours pour audit/debug
    mais on nettoie au-delà. Appelé périodiquement (cleanup task).
    """
    rows = run_sql(
        """DELETE FROM print_jobs
           WHERE tenant_id = :t
             AND status IN ('printed', 'error')
             AND created_at < now() - (:d::text || ' days')::interval
           RETURNING id""",
        {"t": tenant_id, "d": int(keep_days)},
    ) or []
    n = len(rows)
    if n > 0:
        _log.info("Purge %d print jobs > %d jours pour tenant %s", n, keep_days, tenant_id)
    return n
