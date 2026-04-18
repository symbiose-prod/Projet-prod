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
from datetime import date, datetime
from typing import Any

from common._session import current_tenant_id, current_user_id
from db.conn import run_sql

_log = logging.getLogger("ferment.ramasse_history")


def _audit(action: str, tenant_id: str, details: dict[str, Any]) -> None:
    """Fire-and-forget audit log wrapper. Never raises."""
    try:
        from common.audit import log_event
        user_email = None
        try:
            from nicegui import app
            user_email = app.storage.user.get("email")
        except Exception:
            pass
        log_event(
            tenant_id=tenant_id,
            user_email=user_email,
            action=action,
            details=details,
        )
    except Exception:
        _log.debug("Audit log (ramasse/%s) a échoué", action, exc_info=True)


# ─── Comparaison de lignes (pour PDF/email différentiel) ───────────────────

def diff_ramasse_lines(
    old_lines: list[dict[str, Any]] | None,
    new_lines: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Compare deux jeux de lignes de ramasse par leur référence produit.

    Retourne un dict avec 4 clés :
    - ``added``   : lignes présentes dans new mais pas old
    - ``removed`` : lignes présentes dans old mais pas new
    - ``modified``: lignes dont le nombre de cartons a changé (enrichies de ``_old_cartons``)
    - ``unchanged``: lignes identiques (même ref, même cartons)

    Si ``old_lines`` est None ou vide, toutes les lignes new sont considérées comme ``added``.
    La clé de rapprochement est ``ref`` (référence produit).
    """
    if not old_lines:
        return {"added": list(new_lines), "removed": [], "modified": [], "unchanged": []}

    old_by_ref = {str(r.get("ref")): r for r in old_lines}
    new_by_ref = {str(r.get("ref")): r for r in new_lines}

    added: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []

    for ref, new_row in new_by_ref.items():
        old_row = old_by_ref.get(ref)
        if old_row is None:
            added.append(new_row)
        else:
            old_c = int(old_row.get("cartons") or 0)
            new_c = int(new_row.get("cartons") or 0)
            if old_c != new_c:
                modified.append({**new_row, "_old_cartons": old_c})
            else:
                unchanged.append(new_row)

    removed = [old_by_ref[ref] for ref in old_by_ref if ref not in new_by_ref]
    return {"added": added, "removed": removed, "modified": modified, "unchanged": unchanged}


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
    from common.audit import ACTION_RAMASSE_SAVED
    _audit(ACTION_RAMASSE_SAVED, tid, {
        "ramasse_id": rid,
        "destinataire": destinataire,
        "date_ramasse": str(date_ramasse),
        "line_count": len(lines),
        "total_cartons": total_cartons,
        "total_palettes": total_palettes,
    })
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
               status, version, driver_passed, driver_passed_at,
               created_at, updated_at
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
    """Charge une ramasse complète (avec pdf_bytes, lignes, versioning, verrouillage)."""
    tid = tenant_id or current_tenant_id()
    rows = run_sql(
        """
        SELECT id, date_ramasse, destinataire, recipients,
               line_count, total_cartons, total_palettes, total_poids_kg,
               lines, packaging, pdf_bytes, brassin_ids, status,
               version, version_log, previous_lines,
               driver_passed, driver_passed_at, driver_passed_by,
               created_at, updated_at
        FROM ramasse_history
        WHERE id = :rid AND tenant_id = :tid
        LIMIT 1
        """,
        {"rid": ramasse_id, "tid": tid},
    )
    return rows[0] if rows else None


def update_ramasse(
    ramasse_id: str,
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
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Met à jour une ramasse existante en créant une nouvelle version.

    Comportement :
    1. Charge la ramasse courante pour récupérer ``lines`` (devient ``previous_lines``)
       et ``version`` (incrémenté).
    2. Refuse la mise à jour si ``driver_passed = TRUE``.
    3. Remplace ``lines``, totaux, PDF, packaging, brassin_ids avec les nouvelles valeurs.
    4. Incrémente ``version`` et append une entrée dans ``version_log`` pour traçabilité.

    Retourne le record mis à jour ou ``None`` si introuvable / verrouillé.
    """
    tid = tenant_id or current_tenant_id()

    current = get_ramasse(ramasse_id, tenant_id=tid)
    if current is None:
        _log.warning("update_ramasse: ramasse introuvable id=%s", ramasse_id)
        return None
    if current.get("driver_passed"):
        _log.warning("update_ramasse: ramasse verrouillée (chauffeur passé) id=%s", ramasse_id)
        return None

    old_lines = current.get("lines") or []
    old_version = int(current.get("version") or 1)
    new_version = old_version + 1

    # Append au version_log : trace de la version qu'on vient de remplacer
    existing_log = current.get("version_log") or []
    if not isinstance(existing_log, list):
        existing_log = []
    new_log_entry = {
        "version": old_version,
        "saved_at": datetime.utcnow().isoformat() + "Z",
        "lines_count": int(current.get("line_count") or 0),
        "total_cartons": int(current.get("total_cartons") or 0),
        "total_palettes": int(current.get("total_palettes") or 0),
        "total_poids_kg": int(current.get("total_poids_kg") or 0),
    }
    new_version_log = [*existing_log, new_log_entry]

    rows = run_sql(
        """
        UPDATE ramasse_history
        SET date_ramasse    = :dr,
            destinataire    = :dest,
            recipients      = :recip,
            line_count      = :lc,
            total_cartons   = :tc,
            total_palettes  = :tp,
            total_poids_kg  = :tpk,
            lines           = CAST(:lines AS jsonb),
            packaging       = CAST(:pkg AS jsonb),
            pdf_bytes       = :pdf,
            brassin_ids     = :bids,
            version         = :nv,
            version_log     = CAST(:vlog AS jsonb),
            previous_lines  = CAST(:prev AS jsonb),
            updated_at      = now()
        WHERE id = :rid AND tenant_id = :tid AND driver_passed = FALSE
        RETURNING id, version, updated_at
        """,
        {
            "rid": ramasse_id,
            "tid": tid,
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
            "nv": new_version,
            "vlog": json.dumps(new_version_log, default=str, ensure_ascii=False),
            "prev": json.dumps(old_lines, default=str, ensure_ascii=False),
        },
    )
    if not rows:
        _log.warning("update_ramasse: UPDATE n'a retourné aucune ligne id=%s", ramasse_id)
        return None

    _log.info(
        "Ramasse mise à jour: id=%s v%d→v%d cartons=%d",
        ramasse_id, old_version, new_version, total_cartons,
    )
    from common.audit import ACTION_RAMASSE_UPDATED
    _audit(ACTION_RAMASSE_UPDATED, tid, {
        "ramasse_id": ramasse_id,
        "destinataire": destinataire,
        "date_ramasse": str(date_ramasse),
        "version_from": old_version,
        "version_to": new_version,
        "total_cartons": total_cartons,
    })
    return rows[0]


def mark_driver_passed(
    ramasse_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> bool:
    """Marque une ramasse comme livrée (chauffeur passé). Verrouille l'édition.

    Retourne True si la mise à jour a eu lieu, False sinon (introuvable ou déjà marquée).
    Idempotent : ne modifie pas si déjà ``driver_passed = TRUE``.
    """
    tid = tenant_id or current_tenant_id()
    uid = user_id or current_user_id()
    rows = run_sql(
        """
        UPDATE ramasse_history
        SET driver_passed    = TRUE,
            driver_passed_at = now(),
            driver_passed_by = :uid,
            updated_at       = now()
        WHERE id = :rid AND tenant_id = :tid AND driver_passed = FALSE
        RETURNING id
        """,
        {"rid": ramasse_id, "tid": tid, "uid": uid},
    )
    if rows:
        _log.info("Ramasse marquée 'chauffeur passé': id=%s user=%s", ramasse_id, uid)
        from common.audit import ACTION_RAMASSE_DRIVER_PASSED
        _audit(ACTION_RAMASSE_DRIVER_PASSED, tid, {
            "ramasse_id": ramasse_id,
            "user_id": uid,
        })
        return True
    return False


def delete_ramasse(
    ramasse_id: str,
    tenant_id: str | None = None,
) -> bool:
    """Supprime définitivement une ramasse de l'historique.

    Retourne ``True`` si l'enregistrement a bien été supprimé, ``False`` sinon
    (ramasse introuvable ou appartenant à un autre tenant). La suppression est
    **hard delete** — l'enregistrement est retiré de la table, PDF compris.
    """
    tid = tenant_id or current_tenant_id()
    rows = run_sql(
        """
        DELETE FROM ramasse_history
        WHERE id = :rid AND tenant_id = :tid
        RETURNING id
        """,
        {"rid": ramasse_id, "tid": tid},
    )
    if rows:
        _log.info("Ramasse supprimée: id=%s", ramasse_id)
        from common.audit import ACTION_RAMASSE_DELETED
        _audit(ACTION_RAMASSE_DELETED, tid, {"ramasse_id": ramasse_id})
        return True
    _log.warning("delete_ramasse: ramasse introuvable id=%s", ramasse_id)
    return False


def get_last_packaging_for_dest(
    destinataire: str,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Retourne les emballages de la dernière ramasse envoyée pour ce destinataire.

    Utilisé pour proposer des quantités "habituelles" à l'utilisateur au moment
    où il sélectionne un destinataire. Retourne une liste ``[{label, qty, unit}]``
    ou ``[]`` si aucune ramasse passée trouvée (ou si ses emballages sont vides).

    Seules les ramasses avec des emballages renseignés sont considérées.
    """
    tid = tenant_id or current_tenant_id()
    rows = run_sql(
        """
        SELECT packaging
        FROM ramasse_history
        WHERE tenant_id = :tid
          AND destinataire = :dest
          AND packaging IS NOT NULL
          AND jsonb_array_length(packaging) > 0
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"tid": tid, "dest": destinataire},
    )
    if not rows:
        return []
    pkg = rows[0].get("packaging") or []
    if not isinstance(pkg, list):
        return []
    # Filtrer les entrées invalides et ne garder que label/qty/unit
    result: list[dict[str, Any]] = []
    for item in pkg:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        qty = int(item.get("qty") or 0)
        if label and qty > 0:
            result.append({
                "label": label,
                "qty": qty,
                "unit": str(item.get("unit") or "palette"),
            })
    return result


def count_ramasses(tenant_id: str | None = None) -> int:
    """Nombre total de ramasses pour le tenant."""
    tid = tenant_id or current_tenant_id()
    rows = run_sql(
        "SELECT COUNT(*)::int AS n FROM ramasse_history WHERE tenant_id = :tid",
        {"tid": tid},
    )
    return int(rows[0]["n"]) if rows else 0
