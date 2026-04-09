#!/bin/bash
# /usr/local/sbin/

# ============================================================
# Drupal MySQL Backup — Cron Edition
# Run weekly via crontab, keeps the last 7 backups
# ============================================================

# To restore later:
# gunzip < /var/backups/mysql/whatever.sql.gz | mysql -u user -p database

# --- Configuration (edit these) ---
DEFAULTS_FILE="/root/.my-backup.cnf"
DB_NAME="appsecurity"
BACKUP_DIR="/var/backups/mysql"
KEEP=7

# --- Colors (visible in cron mail output) ---
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

# --- Setup ---
if [[ ! -f "$DEFAULTS_FILE" ]]; then
    echo -e "${RED}[ERROR]${NC} Defaults file not found: $DEFAULTS_FILE"
    exit 1
fi
mkdir -p "$BACKUP_DIR" || exit 1
TIMESTAMP=$(/bin/date +"%Y-%m-%d_%H-%M-%S")
FILEPATH="${BACKUP_DIR}/${DB_NAME}_${TIMESTAMP}.sql.gz"
echo "$TIMESTAMP"

# --- Dump ---
echo -e "${CYAN}[INFO]${NC}  Backing up ${DB_NAME} ..."
/bin/mysqldump \
    --defaults-file="$DEFAULTS_FILE" \
    --single-transaction \
    --no-tablespaces \
    --triggers \
    --quick \
    --lock-tables=false \
    "$DB_NAME" | gzip > "$FILEPATH"
if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
    echo -e "${RED}[ERROR]${NC} mysqldump failed"
    rm -f "$FILEPATH"
    exit 1
fi
FILESIZE=$(du -h "$FILEPATH" | cut -f1)
echo -e "${GREEN}[OK]${NC}    ${FILEPATH}  (${FILESIZE})"

# --- Prune old backups, keep the newest $KEEP ---
BACKUPS=($(ls -1t "${BACKUP_DIR}/${DB_NAME}_"*.sql.gz 2>/dev/null))
if (( ${#BACKUPS[@]} > KEEP )); then
    for OLD in "${BACKUPS[@]:$KEEP}"; do
        rm -f "$OLD"
        echo -e "${CYAN}[INFO]${NC}  Removed old backup: $(basename "$OLD")"
    done
fi
echo -e "${GREEN}[OK]${NC}    Done. ${#BACKUPS[@]} backup(s) on disk, keeping last ${KEEP}."
