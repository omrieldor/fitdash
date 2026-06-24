#!/bin/bash
# ============================================================
# Path to Eldorado — Database Backup
# Creates a timestamped copy of your SQLite database.
# Usage: bash backup-db.sh
# ============================================================

DB_PATH="/home/ubuntu/fitdash/instance/dashboard.db"
BACKUP_DIR="/home/ubuntu/backups"

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_PATH" ]; then
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    cp "$DB_PATH" "$BACKUP_DIR/dashboard_${TIMESTAMP}.db"
    echo "Backup saved: $BACKUP_DIR/dashboard_${TIMESTAMP}.db"

    # Keep only last 10 backups
    ls -t "$BACKUP_DIR"/dashboard_*.db | tail -n +11 | xargs -r rm
    echo "Total backups: $(ls "$BACKUP_DIR"/dashboard_*.db | wc -l)"
else
    echo "No database found at $DB_PATH"
fi
