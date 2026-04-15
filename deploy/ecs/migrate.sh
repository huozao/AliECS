#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

if [[ ! -f "$RUNTIME_ENV_FILE" ]]; then
  echo "[migrate] runtime env file not found: $RUNTIME_ENV_FILE" >&2
  exit 1
fi

echo "[migrate] ensure compose stack is started for postgres"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d postgres

echo "[migrate] apply SQL files under /workspace db/migrations"
for sql in /opt/app/db/migrations/*.sql; do
  [[ -f "$sql" ]] || continue
  echo "[migrate] applying $sql"
  docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < "$sql"
done

echo "[migrate] done"
