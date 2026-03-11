"""
common/sync/api_key.py
======================
Gestion des clés API pour l'agent de synchronisation étiquettes.

Les clés sont des tokens haute entropie (secrets.token_urlsafe(32) = 256 bits).
On stocke leur SHA-256 en base (pas besoin de PBKDF2 pour des tokens aléatoires).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any

from db.conn import run_sql

_log = logging.getLogger("ferment.sync")

_KEY_PREFIX = "sk_sync_"


def generate_api_key(
    tenant_id: str,
    created_by: str | None = None,
    label: str = "",
) -> str:
    """Génère une nouvelle clé API sync.

    Retourne la clé brute (affichée une seule fois).
    Le hash SHA-256 est stocké dans sync_api_keys.
    """
    raw_key = _KEY_PREFIX + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    run_sql(
        """INSERT INTO sync_api_keys (tenant_id, key_hash, label, created_by)
           VALUES (:t, :h, :l, :c)""",
        {"t": tenant_id, "h": key_hash, "l": label, "c": created_by},
    )
    _log.info("Sync API key generated for tenant %s (label=%r)", tenant_id, label)
    return raw_key


def verify_api_key(raw_key: str) -> dict[str, Any] | None:
    """Vérifie une clé API sync.

    Retourne {"tenant_id": str, "key_id": str} si valide, None sinon.
    Met à jour last_used en cas de succès.
    """
    if not raw_key or not raw_key.startswith(_KEY_PREFIX):
        return None

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    rows = run_sql(
        """SELECT id, tenant_id FROM sync_api_keys
           WHERE key_hash = :h AND is_active = TRUE""",
        {"h": key_hash},
    )
    if not rows:
        return None

    row = rows[0]
    # Fire-and-forget : maj last_used
    try:
        run_sql(
            "UPDATE sync_api_keys SET last_used = now() WHERE id = :id",
            {"id": row["id"]},
        )
    except Exception:
        _log.debug("Could not update last_used for key %s", row["id"], exc_info=True)

    return {"tenant_id": str(row["tenant_id"]), "key_id": str(row["id"])}


def revoke_api_key(key_id: str) -> bool:
    """Désactive une clé API. Retourne True si trouvée et désactivée."""
    count = run_sql(
        "UPDATE sync_api_keys SET is_active = FALSE WHERE id = :id AND is_active = TRUE",
        {"id": key_id},
    )
    if count:
        _log.info("Sync API key %s revoked", key_id)
    return bool(count)


def list_api_keys(tenant_id: str) -> list[dict[str, Any]]:
    """Liste les clés API actives d'un tenant (sans le hash)."""
    return run_sql(
        """SELECT id, label, is_active, last_used, created_at
           FROM sync_api_keys
           WHERE tenant_id = :t
           ORDER BY created_at DESC""",
        {"t": tenant_id},
    )
