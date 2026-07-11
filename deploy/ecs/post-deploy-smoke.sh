#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
META_FILE="$ROOT_DIR/release-meta.env"

if [[ ! -f "$META_FILE" ]]; then
  echo "[post-deploy] Missing config file: $META_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$META_FILE"

: "${COMPOSE_FILE:?Please set COMPOSE_FILE in release-meta.env}"
: "${RUNTIME_ENV_FILE:?Please set RUNTIME_ENV_FILE in release-meta.env}"
: "${POSTGRES_USER:?Please set POSTGRES_USER in release-meta.env}"
: "${POSTGRES_PASSWORD:?Please set POSTGRES_PASSWORD in release-meta.env}"
: "${POSTGRES_DB:?Please set POSTGRES_DB in release-meta.env}"
: "${HEALTHCHECK_URL:?Please set HEALTHCHECK_URL in release-meta.env}"

compose=(docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE")
POSTGRES_CONTAINER_NAME="${POSTGRES_CONTAINER_NAME:-ecs-postgres-1}"
PSQL_TIMEOUT_SECONDS="${PSQL_TIMEOUT_SECONDS:-30}"

require_service() {
  local service="$1"
  local services
  services="$("${compose[@]}" config --services)"
  if ! grep -Fxq "$service" <<< "$services"; then
    echo "[post-deploy] Missing compose service: $service" >&2
    echo "$services" >&2
    exit 1
  fi
}

check_url() {
  local name="$1"
  local url="$2"
  if curl -fsS "$url" >/dev/null; then
    echo "[post-deploy] OK $name $url"
    return
  fi
  echo "[post-deploy] Failed $name $url" >&2
  exit 1
}

check_table() {
  local table="$1"
  local exists
  exists="$(PGPASSWORD="$POSTGRES_PASSWORD" timeout "$PSQL_TIMEOUT_SECONDS" docker exec -e PGPASSWORD "$POSTGRES_CONTAINER_NAME" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select to_regclass('public.${table}') is not null")"
  if [[ "$exists" != "t" ]]; then
    echo "[post-deploy] Missing database table: $table" >&2
    exit 1
  fi
  echo "[post-deploy] OK table $table"
}

count_table() {
  local table="$1"
  PGPASSWORD="$POSTGRES_PASSWORD" timeout "$PSQL_TIMEOUT_SECONDS" docker exec -e PGPASSWORD "$POSTGRES_CONTAINER_NAME" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from public.${table}"
}

echo "[post-deploy] Checking compose services"
require_service "public-web"
require_service "admin-ui"
require_service "backend-api"
require_service "postgres"
require_service "doc-sync-worker"
require_service "tplus-sync-worker"
require_service "tplus-write-worker"

echo "[post-deploy] Checking container status"
"${compose[@]}" ps

echo "[post-deploy] Checking HTTP entrypoints"
check_url "backend health" "$HEALTHCHECK_URL"
check_url "public-web" "http://127.0.0.1:8080"
check_url "admin-ui" "http://127.0.0.1:8081"

echo "[post-deploy] Checking doc-sync database tables"
check_table "external_sources"
check_table "external_fields"
check_table "external_records"
check_table "sync_runs"
check_table "sync_requests"

echo "[post-deploy] Checking T+ BOM write database tables"
check_table "tplus_bom_drafts"
check_table "tplus_bom_submissions"
check_table "tplus_bom_submission_events"

source_count="$(count_table external_sources)"
run_count="$(count_table sync_runs)"
record_count="$(count_table external_records)"
echo "[post-deploy] doc-sync counts: sources=$source_count runs=$run_count records=$record_count"

if [[ "${POST_DEPLOY_RUN_DOC_SYNC:-false}" == "true" ]]; then
  echo "[post-deploy] Running optional doc-sync smoke"
  if [[ -n "${POST_DEPLOY_DOC_SYNC_PROFILES:-}" ]]; then
    "${compose[@]}" run --rm doc-sync-worker python -m app.main sync-wecom-full --profiles "$POST_DEPLOY_DOC_SYNC_PROFILES"
  else
    "${compose[@]}" run --rm doc-sync-worker python -m app.main sync-wecom-full
  fi
else
  echo "[post-deploy] Skipping optional doc-sync run. Set POST_DEPLOY_RUN_DOC_SYNC=true to enable it."
fi

echo "[post-deploy] Smoke checks passed"
