#!/usr/bin/env python3
"""
ops/backup-db-s3-upload.py
===========================
Upload du **dernier dump PostgreSQL local** vers OVH Object Storage.

Appelé en fin de ``ops/backup-db.sh`` (best-effort, ne bloque pas le
backup local s'il échoue). Permet de survivre à une panne disque OVH du
VPS : les backups sont aussi conservés à distance.

**Pipeline** :

1. Trouve le dernier fichier ``/backups/ferment_*.sql.gz`` (ou
   ``BACKUP_DIR`` env si surchargé).
2. Upload sous la clé ``backups/postgres/YYYY-MM-DD/ferment_*.sql.gz``.
3. Rotation distante : supprime les objets S3 du même préfixe plus
   vieux que ``RETENTION_DAYS`` (par défaut 30, comme le local).

**Configuration requise** dans le ``.env`` du VPS :

.. code-block::

    OVH_S3_ENDPOINT=https://s3.gra.io.cloud.ovh.net
    OVH_S3_REGION=gra
    OVH_S3_BUCKET=ferment-prod-backups   # ou ferment-prod-photos (sous-prefix)
    OVH_S3_ACCESS_KEY=...
    OVH_S3_SECRET_KEY=...

Si ``OVH_S3_BUCKET`` n'est pas configuré, le script log un warning et
sort en code 0 (l'absence de configuration n'est pas une erreur fatale —
le local survit, on est juste sans copie distante).

**Usage manuel** (pour test ou backfill) :

.. code-block:: bash

    sudo -u ubuntu /home/ubuntu/app/.venv/bin/python3 \\
        /home/ubuntu/app/ops/backup-db-s3-upload.py

**Variables d'env honorées** :

- ``BACKUP_DIR`` (défaut ``/home/ubuntu/backups``)
- ``S3_BACKUP_PREFIX`` (défaut ``backups/postgres/``)
- ``RETENTION_DAYS`` (défaut 30)
- ``ENV_FILE`` (défaut ``/home/ubuntu/app/.env``)
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_log = logging.getLogger("backup-db-s3")


# ─── Chargement env (.env du VPS) ──────────────────────────────────────────


def _load_env() -> None:
    """Charge les variables ``OVH_S3_*`` depuis ``.env`` (parsing prudent).

    On évite de sourcer le shell parce que le ``.env`` contient des valeurs
    avec accents/espaces qui font planter ``source`` (cf. backup-db.sh).
    """
    env_file = os.environ.get("ENV_FILE", "/home/ubuntu/app/.env")
    if not os.path.isfile(env_file):
        _log.warning("env file introuvable: %s", env_file)
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith("OVH_S3_"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            # Strip quotes
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


# ─── Trouver le dernier dump local ─────────────────────────────────────────


_DUMP_RE = re.compile(r"^ferment_(\d{8}_\d{6})\.sql\.gz$")


def find_latest_dump(backup_dir: Path) -> Path | None:
    """Cherche le dernier ``ferment_YYYYMMDD_HHMMSS.sql.gz`` dans backup_dir."""
    if not backup_dir.is_dir():
        return None
    candidates: list[tuple[str, Path]] = []
    for p in backup_dir.iterdir():
        if not p.is_file():
            continue
        m = _DUMP_RE.match(p.name)
        if m:
            candidates.append((m.group(1), p))
    if not candidates:
        return None
    candidates.sort()  # ordre lexico = ordre chronologique vu le format
    return candidates[-1][1]


# ─── Upload + rotation ─────────────────────────────────────────────────────


def upload_to_s3(dump_path: Path, prefix: str) -> str:
    """Upload le dump vers S3, retourne la clé utilisée."""
    from common.object_storage import upload_file

    today_dir = datetime.now(UTC).strftime("%Y-%m-%d")
    key = f"{prefix.rstrip('/')}/{today_dir}/{dump_path.name}"
    return upload_file(
        str(dump_path),
        key=key,
        content_type="application/gzip",
        metadata={
            "source-host": os.uname().nodename,
            "source-path": str(dump_path),
        },
    )


def rotate_s3(prefix: str, retention_days: int) -> int:
    """Supprime les objets S3 sous ``prefix`` plus vieux que ``retention_days``.

    Retourne le nombre d'objets supprimés.
    """
    from common.object_storage import delete_object, list_objects

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = 0
    for obj in list_objects(prefix):
        last_modified = obj.get("LastModified")
        if last_modified is None:
            continue
        # boto3 retourne un datetime aware UTC
        if isinstance(last_modified, datetime) and last_modified < cutoff:
            key = str(obj["Key"])
            if delete_object(key):
                deleted += 1
                _log.info("S3 rotation : suppr %s (>%dj)", key, retention_days)
    return deleted


# ─── Main ──────────────────────────────────────────────────────────────────


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )

    _load_env()

    # Importer is_configured APRÈS _load_env (sinon il check avant le set).
    from common.object_storage import OVHStorageError, is_configured

    if not is_configured():
        _log.warning(
            "OVH_S3_* non configuré dans %s — skip upload distant. "
            "Pour activer : voir docstring de ce script.",
            os.environ.get("ENV_FILE", "/home/ubuntu/app/.env"),
        )
        return 0  # pas d'erreur — comportement attendu si pas encore configuré

    backup_dir = Path(os.environ.get("BACKUP_DIR", "/home/ubuntu/backups"))
    prefix = os.environ.get("S3_BACKUP_PREFIX", "backups/postgres/")
    retention_days = int(os.environ.get("RETENTION_DAYS", "30"))

    dump = find_latest_dump(backup_dir)
    if dump is None:
        _log.error("Aucun dump trouvé dans %s", backup_dir)
        return 1

    _log.info("Upload S3 : %s → préfixe %s", dump.name, prefix)
    try:
        key = upload_to_s3(dump, prefix)
        _log.info("Upload OK : key=%s", key)
    except OVHStorageError as exc:
        _log.error("Upload S3 échoué : %s", exc)
        return 2

    try:
        n_deleted = rotate_s3(prefix, retention_days)
        if n_deleted > 0:
            _log.info("Rotation S3 : %d objet(s) supprimé(s) (>%dj)", n_deleted, retention_days)
    except OVHStorageError as exc:
        _log.warning("Rotation S3 échouée (non bloquant) : %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
