#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker/docker-compose.yml}"
MIGRATION_DIR="${MIGRATION_DIR:-$ROOT_DIR/migration}"
BACKUP_MODE="data"

usage() {
  cat <<'EOF'
Usage: ./backup.sh [--full]

Creates migration backups for All Bills Tracker:
- PostgreSQL export
- uploads archive
- app instance archive

Options:
  --full    create a full PostgreSQL schema+data dump instead of data-only
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)
      BACKUP_MODE="full"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

mkdir -p "$MIGRATION_DIR"

echo "Checking Docker stack status..."
docker compose -f "$COMPOSE_FILE" ps

if [[ "$BACKUP_MODE" == "full" ]]; then
  DB_FILE="$MIGRATION_DIR/bill_tracker_full.sql"
  echo "Creating full PostgreSQL backup at $DB_FILE"
  docker compose -f "$COMPOSE_FILE" exec -T db \
    pg_dump -U bill_user -d bill_tracker > "$DB_FILE"
else
  DB_FILE="$MIGRATION_DIR/bill_tracker_data.sql"
  echo "Creating data-only PostgreSQL backup at $DB_FILE"
  docker compose -f "$COMPOSE_FILE" exec -T db \
    pg_dump --data-only --inserts -U bill_user -d bill_tracker > "$DB_FILE"
fi

echo "Backing up uploaded files..."
docker compose -f "$COMPOSE_FILE" exec -T app \
  sh -lc 'cd /app/uploads && tar czf - .' > "$MIGRATION_DIR/uploads.tar.gz"

echo "Backing up app instance data..."
docker compose -f "$COMPOSE_FILE" exec -T app \
  sh -lc 'cd /app/instance && tar czf - .' > "$MIGRATION_DIR/app_instance.tar.gz"

echo
echo "Backup complete."
echo "Created files:"
echo "  $DB_FILE"
echo "  $MIGRATION_DIR/uploads.tar.gz"
echo "  $MIGRATION_DIR/app_instance.tar.gz"
