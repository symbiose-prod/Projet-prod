#!/usr/bin/env bash
# ops/backup-db.sh — Backup PostgreSQL quotidien avec rotation 30 jours.
#
# Format : pg_dump plain SQL compressé (gzip). Conservé parce que :
#  - Restore via ``psql``, pas de dépendance à ``pg_restore``.
#  - Inspectable avec ``zcat | less`` en cas d'urgence.
#  - Rétrocompatible avec l'historique existant dans /backups/ depuis 2026-03.
#
# Usage :
#   ./ops/backup-db.sh                                # BACKUP_DIR=/home/ubuntu/backups par défaut
#   BACKUP_DIR=/backups ./ops/backup-db.sh            # chemin custom
#
# Cron prod (voir crontab -l sur le VPS) :
#   0 3 * * * BACKUP_DIR=/backups /home/ubuntu/app/ops/backup-db.sh >> /backups/backup.log 2>&1
#
# Vérifier/restaurer : voir ops/restore-db.sh.

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/home/ubuntu/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

# Charge uniquement les clés DB_* du .env (éviter de sourcer des valeurs
# non-quotées avec espaces comme EMAIL_SENDER_NAME="Symbiose Kéfir" qui
# feraient planter le shell avec "Kéfir: command not found").
ENV_FILE="${ENV_FILE:-/home/ubuntu/app/.env}"
if [[ -f "$ENV_FILE" ]]; then
    # Extraction robuste : strip guillemets éventuels autour des valeurs.
    while IFS='=' read -r key value; do
        # Retire les guillemets simples/doubles optionnels autour de value
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"
        export "$key=$value"
    done < <(grep -E '^DB_(DATABASE|USERNAME|HOST|PORT)=' "$ENV_FILE" || true)
fi

DB_NAME="${DB_DATABASE:-whole-tomato-leopard}"
DB_USER="${DB_USERNAME:-shark}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FILENAME="ferment_${TIMESTAMP}.sql.gz"
BACKUP_PATH="${BACKUP_DIR}/${FILENAME}"

# ─── Backup ──────────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Backup PostgreSQL : ${DB_NAME} -> ${BACKUP_PATH}"

# sudo -u postgres : le user postgres a l'accès local par peer auth (pas de password).
# Format SQL plain (pas --format=custom) pour rétrocompat avec les backups
# historiques dans /backups/ et restore via psql.
sudo -u postgres pg_dump "${DB_NAME}" | gzip > "${BACKUP_PATH}"

# Vérifier que le backup n'est pas vide (anomalie détectable tôt)
SIZE=$(stat -c%s "${BACKUP_PATH}" 2>/dev/null || stat -f%z "${BACKUP_PATH}")
if [[ "${SIZE}" -lt 1000 ]]; then
    echo "[$(date -Iseconds)] ERREUR : backup trop petit (${SIZE} octets) — abandon." >&2
    rm -f "${BACKUP_PATH}"
    exit 1
fi

SIZE_H=$(du -h "${BACKUP_PATH}" | cut -f1)
echo "[$(date -Iseconds)] Backup OK : ${FILENAME} (${SIZE_H})"

# ─── Rotation : supprimer les backups de plus de N jours ─────────────────────
DELETED=$(find "$BACKUP_DIR" -name "ferment_*.sql.gz" -mtime +"$RETENTION_DAYS" -delete -print | wc -l)
if [[ "$DELETED" -gt 0 ]]; then
    echo "[$(date -Iseconds)] Rotation : ${DELETED} ancien(s) backup(s) supprime(s) (> ${RETENTION_DAYS}j)"
fi

# ─── Copie distante OVH Object Storage (best-effort) ────────────────────────
# Sécurité contre une panne disque OVH du VPS : on uploade le dernier dump
# vers OVH Object Storage. Skip silencieusement si OVH_S3_* pas configuré
# (cf. docstring de backup-db-s3-upload.py). Ne BLOQUE PAS le backup local
# en cas d'échec d'upload — on a déjà la copie locale qui est l'essentiel.
APP_DIR="${APP_DIR:-/home/ubuntu/app}"
VENV_PY="${VENV_PY:-${APP_DIR}/.venv/bin/python3}"
S3_UPLOAD_SCRIPT="${APP_DIR}/ops/backup-db-s3-upload.py"
if [[ -x "$VENV_PY" && -f "$S3_UPLOAD_SCRIPT" ]]; then
    echo "[$(date -Iseconds)] Upload S3 : $S3_UPLOAD_SCRIPT"
    # PYTHONPATH pour que le script trouve common/object_storage
    if PYTHONPATH="$APP_DIR" BACKUP_DIR="$BACKUP_DIR" RETENTION_DAYS="$RETENTION_DAYS" \
        "$VENV_PY" "$S3_UPLOAD_SCRIPT"; then
        echo "[$(date -Iseconds)] Upload S3 termine."
    else
        echo "[$(date -Iseconds)] WARN : upload S3 a echoue (non bloquant — backup local OK)." >&2
    fi
else
    echo "[$(date -Iseconds)] Upload S3 : venv ou script introuvable, skip (venv=$VENV_PY, script=$S3_UPLOAD_SCRIPT)."
fi

echo "[$(date -Iseconds)] Backup termine."
