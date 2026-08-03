#!/bin/bash
# Backup production database — run daily via cron
# Usage: ./scripts/backup.sh

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-.backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
TIMESTAMP=$(date +%Y-%m-%d)
LOG_FILE="$BACKUP_DIR/backup.log"

mkdir -p "$BACKUP_DIR"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Load env
set -a
source .env.prod 2>/dev/null || {
    log "ERROR: .env.prod not found"
    exit 1
}
set +a

log "Starting backup..."

# Backup main database
log "Backing up ohana database..."
docker compose -f docker-compose.prod.yml exec -T postgres pg_dump \
    -Fc -U ohana -d ohana \
    > "$BACKUP_DIR/ohana_$TIMESTAMP.dump"
log "✓ ohana backup: $BACKUP_DIR/ohana_$TIMESTAMP.dump"

# Backup Langfuse database (weekly)
if [[ $(date +%u) -eq 1 ]]; then
    log "Backing up langfuse database (Monday)..."
    docker compose -f docker-compose.prod.yml exec -T langfuse-db pg_dump \
        -Fc -U postgres -d langfuse \
        > "$BACKUP_DIR/langfuse_$TIMESTAMP.dump"
    log "✓ langfuse backup: $BACKUP_DIR/langfuse_$TIMESTAMP.dump"
fi

# Rotate old backups
log "Rotating backups older than $RETENTION_DAYS days..."
find "$BACKUP_DIR" -name "*.dump" -mtime +$RETENTION_DAYS -delete
log "✓ Rotation complete"

log "Backup finished"
