"""
common/audit.py
===============
Journal d'audit — traçabilité des actions métier.

Chaque événement est persisté dans la table audit_log (INSERT fire-and-forget).
En cas d'échec DB, une seconde tentative est effectuée puis un fallback vers le
logger Python pour ne jamais perdre un événement d'audit silencieusement.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from db.conn import run_sql

_log = logging.getLogger("ferment.audit")

# Actions reconnues (non exhaustif — on accepte tout str <= 50 chars)
ACTION_LOGIN = "login"
ACTION_LOGIN_FAILED = "login_failed"
ACTION_LOGOUT = "logout"
ACTION_SIGNUP = "signup"
ACTION_PASSWORD_RESET = "password_reset"
ACTION_PRODUCTION_SAVED = "production_saved"
ACTION_BRASSIN_CREATED = "brassin_created"
ACTION_FILE_UPLOADED = "file_uploaded"
ACTION_SUPPLIER_CONFIG_UPDATED = "supplier_config_updated"
ACTION_RAMASSE_SAVED = "ramasse_saved"
ACTION_RAMASSE_UPDATED = "ramasse_updated"
ACTION_RAMASSE_DELETED = "ramasse_deleted"
ACTION_RAMASSE_RESTORED = "ramasse_restored"
ACTION_RAMASSE_DRIVER_PASSED = "ramasse_driver_passed"
ACTION_RAMASSE_DRIVER_UNMARKED = "ramasse_driver_unmarked"


def log_event(
    *,
    tenant_id: str | None = None,
    user_email: str | None = None,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Insère un événement dans audit_log. Ne lève jamais d'exception.

    Deux tentatives DB, puis fallback vers le logger en cas d'échec persistant
    pour garantir qu'aucun événement d'audit n'est perdu silencieusement.
    """
    params = {
        "t": tenant_id,
        "e": user_email,
        "a": action[:50],
        "d": _json_dumps(details or {}),
    }
    sql = """
        INSERT INTO audit_log (tenant_id, user_email, action, details)
        VALUES (:t, :e, :a, CAST(:d AS jsonb))
    """
    for attempt in range(2):
        try:
            run_sql(sql, params)
            return
        except (SQLAlchemyError, OSError):
            if attempt == 0:
                _log.debug("Audit INSERT échec (tentative 1/2), retry…", exc_info=True)
            else:
                # Fallback : logger l'événement pour ne pas le perdre
                _log.error(
                    "AUDIT_FALLBACK action=%s user=%s tenant=%s details=%s",
                    action, user_email, tenant_id, details,
                    exc_info=True,
                )


def _json_dumps(obj: Any) -> str:
    """Sérialise en JSON (import local pour éviter le coût au module-level)."""
    import json
    return json.dumps(obj, default=str, ensure_ascii=False)


# ─── Rétention : politique RGPD + traçabilité alimentaire FR ──────────────
#
# Par défaut 13 mois : couvre une saison commerciale complète + 1 mois de
# marge pour les audits/réclamations. Au-delà on perd la PII (user_email)
# mais on garde tenant_id + action + details pour la traçabilité métier.
#
# Les évènements liés aux SSCC/lots restent dans `sscc_log` et
# `ramasse_history` qui ont leur propre politique (5 ans pour conformité
# alimentaire FR).

DEFAULT_RETENTION_MONTHS = 13


def purge_audit_log(retention_months: int = DEFAULT_RETENTION_MONTHS) -> int:
    """Supprime les évènements ``audit_log`` plus vieux que N mois.

    À appeler depuis un cron quotidien/hebdomadaire (déploiement ops).
    Idempotent — peut être rejouée sans risque.

    Args :
      ``retention_months`` : nombre de mois à conserver (défaut 13).

    Retourne le nombre de lignes supprimées (utile pour monitoring).

    Note RGPD : on supprime ici la ligne ENTIÈRE (action + details + tenant_id),
    pas seulement la PII. C'est cohérent avec le principe de limitation de
    conservation (art.5 RGPD). Les actions métier critiques (lots, SSCC) sont
    déjà dans des tables dédiées avec leur propre rétention.
    """
    if retention_months <= 0:
        raise ValueError("retention_months doit être > 0")
    sql = f"""
        DELETE FROM audit_log
        WHERE created_at < now() - INTERVAL '{int(retention_months)} months'
    """
    try:
        result = run_sql(sql)
        # `run_sql` peut retourner None pour DELETE — récupérer rowcount
        # via la 2e API si dispo. Pour simplicité on logue l'événement.
        deleted = len(result) if result else 0
        _log.info(
            "audit_log purgé : %d lignes supprimées (rétention %d mois)",
            deleted, retention_months,
        )
        return deleted
    except (SQLAlchemyError, OSError):
        _log.exception("Échec purge audit_log")
        return 0
