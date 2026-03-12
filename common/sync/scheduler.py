"""
common/sync/scheduler.py
========================
Scheduler quotidien pour la sync étiquettes.

Lance collect_label_data() tous les jours à l'heure configurée (par tenant)
et crée une opération REPLACE_ALL dans sync_operations.

L'heure est stockée dans tenants.sync_schedule_hour (défaut: 5h, heure Paris).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from dateutil.tz import gettz

_log = logging.getLogger("ferment.sync")
_PARIS_TZ = gettz("Europe/Paris")

# Fallback si impossible de lire la DB
_DEFAULT_HOUR = 5
_DEFAULT_MINUTE = 0


def _get_schedule_hour() -> int:
    """Récupère l'heure de sync depuis la DB (premier tenant)."""
    try:
        from db.conn import run_sql
        rows = run_sql(
            "SELECT sync_schedule_hour FROM tenants LIMIT 1", {},
        )
        if rows and rows[0].get("sync_schedule_hour") is not None:
            return int(rows[0]["sync_schedule_hour"])
    except Exception:
        _log.debug("Impossible de lire sync_schedule_hour, fallback %dh", _DEFAULT_HOUR)
    return _DEFAULT_HOUR


def _seconds_until_target(hour: int | None = None) -> float:
    """Calcule le nombre de secondes jusqu'au prochain créneau cible (heure Paris)."""
    if hour is None:
        hour = _get_schedule_hour()
    now = dt.datetime.now(_PARIS_TZ)
    target = now.replace(hour=hour, minute=_DEFAULT_MINUTE, second=0, microsecond=0)
    if now >= target:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


async def _run_sync_job() -> dict | None:
    """Exécute la collecte EasyBeer et crée une opération sync.

    Les appels EasyBeer sont bloquants (requests) → exécutés dans un thread pool.
    """
    loop = asyncio.get_event_loop()

    try:
        # Import tardif pour éviter les imports circulaires au démarrage
        from common.sync.collector import collect_label_data
        from common.sync import create_sync_operation

        products = await loop.run_in_executor(None, collect_label_data)

        if not products:
            _log.warning("Sync scheduler : aucun produit collecté, opération non créée")
            return None

        # Récupérer le tenant_id (en production : Symbiose Kéfir)
        tenant_id = _get_default_tenant_id()
        if not tenant_id:
            _log.error("Sync scheduler : impossible de déterminer le tenant_id")
            return None

        op = create_sync_operation(products, tenant_id=tenant_id, triggered_by="scheduler")
        _log.info("Sync scheduler OK : opération #%s, %d produits", op["id"], op["product_count"])
        return op

    except Exception:
        _log.exception("Erreur lors de la sync scheduler quotidienne")
        return None


def _get_default_tenant_id() -> str | None:
    """Récupère le tenant_id du premier tenant (mono-tenant en production)."""
    try:
        from db.conn import run_sql
        rows = run_sql("SELECT id FROM tenants LIMIT 1", {})
        if rows:
            return str(rows[0]["id"])
    except Exception:
        _log.exception("Erreur récupération tenant_id pour scheduler")
    return None


async def daily_sync_loop() -> None:
    """Boucle infinie : lance la sync tous les jours à l'heure configurée.

    Relit l'heure depuis la DB à chaque itération pour prendre en compte
    les changements faits depuis l'UI /sync.
    """
    while True:
        hour = _get_schedule_hour()
        wait = _seconds_until_target(hour)
        next_run = dt.datetime.now(_PARIS_TZ) + dt.timedelta(seconds=wait)
        _log.info(
            "Prochaine sync étiquettes dans %.0f s (à %02dh%02d le %s)",
            wait, hour, _DEFAULT_MINUTE, next_run.strftime("%d/%m"),
        )
        await asyncio.sleep(wait)

        _log.info("=== Lancement sync quotidienne étiquettes ===")
        await _run_sync_job()
