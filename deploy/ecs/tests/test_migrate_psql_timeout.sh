#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MIGRATE_SH="$ROOT_DIR/migrate.sh"

if ! grep -q 'PGPASSWORD=.*docker exec' "$MIGRATE_SH"; then
  echo "migrate.sh must pass PGPASSWORD into docker exec psql calls" >&2
  exit 1
fi

if ! grep -q 'timeout.*docker exec' "$MIGRATE_SH"; then
  echo "migrate.sh must wrap docker exec psql calls with timeout" >&2
  exit 1
fi

if grep -q 'docker compose .* exec .*psql' "$MIGRATE_SH"; then
  echo "migrate.sh must not use docker compose exec for psql calls" >&2
  exit 1
fi
