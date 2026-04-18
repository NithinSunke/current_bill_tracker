#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker/docker-compose.yml}"
MIGRATION_DIR="${MIGRATION_DIR:-$ROOT_DIR/migration}"
DB_MODE="auto"

usage() {
  cat <<'EOF'
Usage: ./restore.sh [--data-only | --full]

Restores All Bills Tracker migration backups:
- uploads archive
- app instance archive
- PostgreSQL backup

Options:
  --data-only   restore migration/bill_tracker_data.sql
  --full        restore migration/bill_tracker_full.sql
  default       auto-detect backup file, preferring full then data-only
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-only)
      DB_MODE="data"
      shift
      ;;
    --full)
      DB_MODE="full"
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

UPLOADS_ARCHIVE="$MIGRATION_DIR/uploads.tar.gz"
INSTANCE_ARCHIVE="$MIGRATION_DIR/app_instance.tar.gz"
FULL_DUMP="$MIGRATION_DIR/bill_tracker_full.sql"
DATA_DUMP="$MIGRATION_DIR/bill_tracker_data.sql"

if [[ ! -f "$UPLOADS_ARCHIVE" ]]; then
  echo "Missing uploads archive: $UPLOADS_ARCHIVE" >&2
  exit 1
fi

if [[ ! -f "$INSTANCE_ARCHIVE" ]]; then
  echo "Missing app instance archive: $INSTANCE_ARCHIVE" >&2
  exit 1
fi

case "$DB_MODE" in
  full)
    DB_FILE="$FULL_DUMP"
    ;;
  data)
    DB_FILE="$DATA_DUMP"
    ;;
  auto)
    if [[ -f "$FULL_DUMP" ]]; then
      DB_FILE="$FULL_DUMP"
    elif [[ -f "$DATA_DUMP" ]]; then
      DB_FILE="$DATA_DUMP"
    else
      echo "No PostgreSQL backup found in $MIGRATION_DIR" >&2
      exit 1
    fi
    ;;
esac

if [[ ! -f "$DB_FILE" ]]; then
  echo "Missing database backup: $DB_FILE" >&2
  exit 1
fi

echo "Starting Docker stack..."
docker compose -f "$COMPOSE_FILE" up -d --build

echo "Current Docker stack status:"
docker compose -f "$COMPOSE_FILE" ps

echo "Restoring uploaded files..."
cat "$UPLOADS_ARCHIVE" | docker compose -f "$COMPOSE_FILE" exec -T app \
  sh -lc 'cd /app/uploads && tar xzf -'

echo "Restoring app instance data..."
cat "$INSTANCE_ARCHIVE" | docker compose -f "$COMPOSE_FILE" exec -T app \
  sh -lc 'cd /app/instance && tar xzf -'

echo "Restoring PostgreSQL backup from $DB_FILE"
cat "$DB_FILE" | docker compose -f "$COMPOSE_FILE" exec -T db \
  psql -U bill_user -d bill_tracker

echo "Restarting app container..."
docker compose -f "$COMPOSE_FILE" restart app

echo
echo "Restore complete."
echo "Verify with:"
echo "  docker compose -f \"$COMPOSE_FILE\" ps"
echo "  docker compose -f \"$COMPOSE_FILE\" logs --tail=100 app"
echo "  docker compose -f \"$COMPOSE_FILE\" logs --tail=100 db"
