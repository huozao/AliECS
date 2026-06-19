#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MIGRATE_SH="$ROOT_DIR/migrate.sh"

if ! grep -q 'PGPASSWORD=.*docker compose' "$MIGRATE_SH"; then
  echo "migrate.sh must pass PGPASSWORD into docker compose psql calls" >&2
  exit 1
fi

if ! grep -q 'timeout.*docker compose' "$MIGRATE_SH"; then
  echo "migrate.sh must wrap docker compose psql calls with timeout" >&2
  exit 1
fi
