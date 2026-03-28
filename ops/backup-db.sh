#!/usr/bin/env bash
# ops/backup-db.sh — Backup PostgreSQL quotidien avec rotation 30 jours
#
# Usage :
#   ./ops/backup-db.sh                    # backup local dans /home/ubuntu/backups/
#   BACKUP_DIR=/tmp/backups ./ops/backup-db.sh   # chemin custom
#
# Cron recommandé (ajout via `sudo crontab -e`) :
#   0 2 * * * /home/ubuntu/app/ops/backup-db.sh >> /var/log/ferment-backup.log 2>&1

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/home/ubuntu/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

# Charge les variables d'env de l'app si disponibles
ENV_FILE="${ENV_FILE:-/home/ubuntu/app/.env}"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

DB_NAME="${DB_DATABASE:-whole-tomato-leopard}"
DB_USER="${DB_USERNAME:-shark}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FILENAME="ferment_${DB_NAME}_${TIMESTAMP}.sql.gz"

# ─── Backup ──────────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Backup PostgreSQL : ${DB_NAME} -> ${BACKUP_DIR}/${FILENAME}"

pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    --no-password \
    --format=custom \
    "$DB_NAME" \
  | gzip > "${BACKUP_DIR}/${FILENAME}"

SIZE=$(du -h "${BACKUP_DIR}/${FILENAME}" | cut -f1)
echo "[$(date -Iseconds)] Backup OK : ${FILENAME} (${SIZE})"

# ─── Rotation : supprimer les backups de plus de N jours ─────────────────────
DELETED=$(find "$BACKUP_DIR" -name "ferment_*.sql.gz" -mtime +"$RETENTION_DAYS" -delete -print | wc -l)
if [[ "$DELETED" -gt 0 ]]; then
    echo "[$(date -Iseconds)] Rotation : ${DELETED} ancien(s) backup(s) supprime(s) (> ${RETENTION_DAYS}j)"
fi

echo "[$(date -Iseconds)] Backup termine."
