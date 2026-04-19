#!/usr/bin/env bash
# ops/restore-db.sh — Restore testable d'un backup pg_dump.
#
# Comportement par défaut : "test" — crée une DB temporaire, restaure,
# valide la présence des tables critiques, drop la DB temporaire. Retour
# non-zéro si l'une des étapes échoue.
#
# Utilisé :
#   - En manuel (ops drill : `ops/restore-db.sh latest`).
#   - En cron hebdo (valide que les backups restent restaurables — idéal).
#
# Usage :
#   ops/restore-db.sh                              # restaure le + récent, valide, drop
#   ops/restore-db.sh latest                       # idem
#   ops/restore-db.sh /backups/ferment_xxx.sql.gz  # backup spécifique
#   ops/restore-db.sh latest --keep                # garde la DB de test pour inspection
#   ops/restore-db.sh latest --target my_db_name   # restaure dans une DB existante
#                                                  # (DANGEROUS, pour un vrai DR scenario)
#
# Dépendances : sudo, psql, gunzip.

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_PATH=""
KEEP_DB=false
CUSTOM_TARGET=""

# Tables métier dont on vérifie qu'elles contiennent au moins 1 row après restore.
# Si l'une est vide, on signale (warning, pas error — une prod fraîche pourrait
# légitimement avoir audit_log vide).
CRITICAL_TABLES=(tenants users)

# Tables attendues mais qui peuvent être vides sans alarme.
EXPECTED_TABLES=(audit_log ramasse_history production_proposals eb_cache user_sessions)

# ─── Parse args ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        latest|"")
            # Pick most recent backup file
            BACKUP_PATH=$(find "$BACKUP_DIR" -name 'ferment_*.sql.gz' -type f \
                -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
            if [[ -z "$BACKUP_PATH" ]]; then
                echo "ERREUR : aucun backup trouvé dans ${BACKUP_DIR}" >&2
                exit 1
            fi
            shift
            ;;
        --keep)
            KEEP_DB=true
            shift
            ;;
        --target)
            CUSTOM_TARGET="$2"
            shift 2
            ;;
        -h|--help)
            grep -E '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            if [[ -f "$1" ]]; then
                BACKUP_PATH="$1"
                shift
            else
                echo "ERREUR : argument inconnu ou fichier manquant : $1" >&2
                exit 1
            fi
            ;;
    esac
done

# Si aucun arg et BACKUP_PATH toujours vide → fallback "latest"
if [[ -z "$BACKUP_PATH" ]]; then
    BACKUP_PATH=$(find "$BACKUP_DIR" -name 'ferment_*.sql.gz' -type f \
        -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    if [[ -z "$BACKUP_PATH" ]]; then
        echo "ERREUR : aucun backup trouvé dans ${BACKUP_DIR}" >&2
        exit 1
    fi
fi

if [[ ! -f "$BACKUP_PATH" ]]; then
    echo "ERREUR : backup introuvable : $BACKUP_PATH" >&2
    exit 1
fi

# ─── Préparer la DB cible ────────────────────────────────────────────────────
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
if [[ -n "$CUSTOM_TARGET" ]]; then
    TARGET_DB="$CUSTOM_TARGET"
    echo "[$(date -Iseconds)] ⚠️  Restore dans DB existante : ${TARGET_DB}"
    echo "[$(date -Iseconds)] ⚠️  Cela écrase toutes les données actuelles."
    echo -n "Confirmer (tape 'oui' en majuscules OUI) : "
    read -r CONFIRM
    if [[ "$CONFIRM" != "OUI" ]]; then
        echo "Abandon."
        exit 1
    fi
else
    TARGET_DB="ferment_restore_test_${TIMESTAMP}"
fi

SIZE=$(du -h "$BACKUP_PATH" | cut -f1)
echo "[$(date -Iseconds)] Backup source : ${BACKUP_PATH} (${SIZE})"
echo "[$(date -Iseconds)] DB cible      : ${TARGET_DB}"

# ─── Cleanup on exit (sauf --keep ou --target) ───────────────────────────────
cleanup() {
    local exit_code=$?
    if [[ -z "$CUSTOM_TARGET" ]] && [[ "$KEEP_DB" == "false" ]]; then
        echo "[$(date -Iseconds)] Nettoyage : drop ${TARGET_DB}"
        sudo -u postgres psql -q -c "DROP DATABASE IF EXISTS \"${TARGET_DB}\";" 2>/dev/null || true
    elif [[ "$KEEP_DB" == "true" ]]; then
        echo "[$(date -Iseconds)] DB conservée : ${TARGET_DB}"
        echo "[$(date -Iseconds)] Pour la supprimer manuellement :"
        echo "    sudo -u postgres psql -c \"DROP DATABASE \\\"${TARGET_DB}\\\";\""
    fi
    exit "$exit_code"
}
trap cleanup EXIT

# ─── Create DB temporaire ────────────────────────────────────────────────────
if [[ -z "$CUSTOM_TARGET" ]]; then
    echo "[$(date -Iseconds)] Création DB ${TARGET_DB}..."
    sudo -u postgres psql -q -c "CREATE DATABASE \"${TARGET_DB}\";" || {
        echo "ERREUR : création DB échouée" >&2
        exit 2
    }
fi

# ─── Restore ─────────────────────────────────────────────────────────────────
echo "[$(date -Iseconds)] Restore en cours..."
START=$(date +%s)
# gunzip -> psql. Le dump plain SQL fait ``CREATE TABLE ... INSERT ...``,
# toutes les tables atterrissent dans public schema.
if ! gunzip -c "$BACKUP_PATH" | sudo -u postgres psql -q -d "$TARGET_DB" > /dev/null 2>&1; then
    # Retry une fois avec le log visible pour diagnostic
    echo "[$(date -Iseconds)] Restore échoué, relance avec logs visibles..."
    gunzip -c "$BACKUP_PATH" | sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$TARGET_DB" 2>&1 | tail -20
    exit 3
fi
ELAPSED=$(($(date +%s) - START))
echo "[$(date -Iseconds)] Restore OK en ${ELAPSED}s"

# ─── Validation : tables présentes + row counts ──────────────────────────────
echo "[$(date -Iseconds)] Validation des tables critiques..."
FAILURES=0
WARNINGS=0

check_table() {
    local table=$1
    local min_rows=${2:-0}

    local count
    count=$(sudo -u postgres psql -qtAX -d "$TARGET_DB" -c \
        "SELECT COUNT(*) FROM \"${table}\";" 2>/dev/null) || count="ERR"

    if [[ "$count" == "ERR" ]]; then
        echo "  ❌ ${table}            → table MANQUANTE"
        FAILURES=$((FAILURES + 1))
        return
    fi

    if [[ "$count" -lt "$min_rows" ]]; then
        echo "  ⚠️  ${table}            → ${count} rows (attendu >= ${min_rows})"
        WARNINGS=$((WARNINGS + 1))
    else
        echo "  ✅ ${table}            → ${count} rows"
    fi
}

for t in "${CRITICAL_TABLES[@]}"; do
    check_table "$t" 1   # au moins 1 row attendu
done
for t in "${EXPECTED_TABLES[@]}"; do
    check_table "$t" 0   # table présente suffit
done

# ─── Résumé ──────────────────────────────────────────────────────────────────
echo ""
echo "[$(date -Iseconds)] Résumé restore :"
echo "  Backup     : $(basename "$BACKUP_PATH")"
echo "  Durée      : ${ELAPSED}s"
echo "  DB cible   : ${TARGET_DB}"
echo "  Échecs     : ${FAILURES}"
echo "  Warnings   : ${WARNINGS}"

if [[ "$FAILURES" -gt 0 ]]; then
    echo ""
    echo "❌ RESTORE ÉCHOUÉ — ${FAILURES} table(s) critique(s) manquante(s) ou vide(s)."
    exit 4
fi

echo ""
echo "✅ RESTORE OK — le backup peut être utilisé en cas d'urgence."
