#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/local/docker-compose.local.yml"
ENV_FILE="$REPO_ROOT/local/.env.local"
ENV_EXAMPLE="$REPO_ROOT/local/.env.local.example"
RUN_DOC_SYNC=0
DOC_SYNC_PROFILES=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --run-doc-sync)
      RUN_DOC_SYNC=1
      shift
      ;;
    --profiles)
      DOC_SYNC_PROFILES="$2"
      shift 2
      ;;
    *)
      echo "[local-smoke] Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

step() {
  echo
  echo "[local-smoke] $1"
}

show_recent_logs() {
  echo
  echo "[local-smoke] Recent container logs:"
  compose logs --tail=120 || true
}

fail_with_logs() {
  echo
  echo "[local-smoke] FAILED: $1" >&2
  show_recent_logs
  echo
  echo "[local-smoke] To clean up manually, run:"
  echo "docker compose -f local/docker-compose.local.yml down"
  exit 1
}

check_http() {
  local url="$1"
  local required="$2"
  local code

  code="$(curl -fsS -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || true)"
  if [[ "$code" =~ ^[23] ]]; then
    echo "[local-smoke] OK $url -> HTTP $code"
    return 0
  fi

  if [[ "$required" == "required" ]]; then
    fail_with_logs "$url request failed or returned an unexpected status (HTTP ${code:-none})"
  fi

  echo "[local-smoke] Optional check failed: $url is not available (HTTP ${code:-none})."
  return 0
}

env_value() {
  local name="$1"
  local default="$2"
  local line
  line="$(grep -E "^[[:space:]]*${name}[[:space:]]*=" "$ENV_FILE" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    echo "$default"
    return 0
  fi
  line="${line#*=}"
  line="${line%\"}"
  line="${line#\"}"
  line="${line%\'}"
  line="${line#\'}"
  echo "$line"
}

wait_postgres() {
  local pg_user
  local pg_db
  pg_user="$(env_value POSTGRES_USER app)"
  pg_db="$(env_value POSTGRES_DB app)"

  for _ in $(seq 1 60); do
    if compose exec -T postgres pg_isready -U "$pg_user" -d "$pg_db" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done

  fail_with_logs "postgres did not become ready in time"
}

apply_local_migrations() {
  local pg_user
  local pg_db
  pg_user="$(env_value POSTGRES_USER app)"
  pg_db="$(env_value POSTGRES_DB app)"

  step "Applying local SQL migrations"
  shopt -s nullglob
  local sql_files=("$REPO_ROOT"/db/migrations/*.sql)
  for sql_file in "${sql_files[@]}"; do
    echo "[local-smoke] Applying $(basename "$sql_file")"
    local container_path="/tmp/aliecs-migration-$(basename "$sql_file")"
    compose cp "$sql_file" "postgres:$container_path" \
      || fail_with_logs "copy migration failed: $(basename "$sql_file")"
    compose exec -T postgres psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 -f "$container_path" \
      || fail_with_logs "migration failed: $(basename "$sql_file")"
  done
}

env_name_exists() {
  local pattern="$1"
  grep -Eq "$pattern" "$ENV_FILE"
}

doc_sync_config_exists() {
  { env_name_exists "^[[:space:]]*WECOM_ENV_PROFILES[[:space:]]*=" || env_name_exists "^[[:space:]]*WECOM_.*_CORP_ID[[:space:]]*="; } \
    && env_name_exists "^[[:space:]]*WECOM_.*_CORP_ID[[:space:]]*=" \
    && env_name_exists "^[[:space:]]*WECOM_.*_APP_SECRET" \
    && { env_name_exists "^[[:space:]]*WEDOC_.*_DOCID[[:space:]]*=" || env_name_exists "^[[:space:]]*SMARTSHEET_.*_ID[[:space:]]*="; }
}

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[local-smoke] Missing local/.env.local."
  echo "[local-smoke] Copy the local example and keep only local test values:"
  echo "cp local/.env.local.example local/.env.local"
  echo "[local-smoke] Example file: $ENV_EXAMPLE"
  exit 1
fi

step "Checking Docker Compose config"
compose config >/dev/null

step "Starting local services"
compose up --build -d || fail_with_logs "docker compose up failed"

step "Waiting for postgres"
wait_postgres
apply_local_migrations

step "Waiting for backend-api health"
healthy=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 http://localhost:8000/healthz >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done

if [[ "$healthy" != "1" ]]; then
  fail_with_logs "backend-api did not pass /healthz in time"
fi

step "Checking local entrypoints"
check_http "http://localhost:8080" required
check_http "http://localhost:8081" required
check_http "http://localhost:8000/healthz" required
check_http "http://localhost:8000/api/healthz" optional

if [[ "$RUN_DOC_SYNC" == "1" ]]; then
  step "Checking doc-sync-worker command"
  if doc_sync_config_exists; then
    sync_args=(run --rm doc-sync-worker python -m app.main sync-wecom-full)
    if [[ -n "$DOC_SYNC_PROFILES" ]]; then
      sync_args+=(--profiles "$DOC_SYNC_PROFILES")
    fi
    compose "${sync_args[@]}" || fail_with_logs "doc-sync-worker real sync failed"
  else
    echo "[local-smoke] Skipping real WeCom sync: env file does not contain WECOM profiles, corp id, app secret and docid variable names."
    echo "[local-smoke] To run real sync, fill local/.env.local or pass --env-file with a file containing WECOM_* and WEDOC_/SMARTSHEET_* variables."
  fi
else
  step "Checking doc-sync-worker help"
  compose run --rm doc-sync-worker python -m app.main --help || fail_with_logs "doc-sync-worker help failed"
fi

step "Container status"
compose ps || fail_with_logs "docker compose ps failed"

echo
echo "[local-smoke] Local smoke test finished."
echo "[local-smoke] To clean up manually, run:"
echo "docker compose -f local/docker-compose.local.yml down"
