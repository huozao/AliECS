#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$ROOT_DIR/../.." && pwd)"
META_FILE="$ROOT_DIR/release-meta.env"

redact() {
  sed -E \
    -e 's#(postgres(ql)?://)[^:@/]+:[^@/]+@#\1***:***@#g' \
    -e 's#([Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Tt][Oo][Kk][Ee][Nn]|[Ss][Ee][Cc][Rr][Ee][Tt]|ACCESS_KEY|APP_SECRET|CORP_ID)=([^[:space:]]+)#\1=***#g' \
    -e 's#(access_token|corpsecret|token|secret)=([^&[:space:]]+)#\1=***#g'
}

section() {
  echo
  echo "===== $* ====="
}

safe_run() {
  local label="$1"
  shift
  section "$label"
  if ! "$@" 2>&1 | redact; then
    echo "[diagnostics] command failed: $*" | redact
  fi
}

print_env_presence() {
  local name="$1"
  local value="${!name-}"
  if [[ -n "$value" ]]; then
    echo "$name=SET len=${#value}"
  else
    echo "$name=MISSING"
  fi
}

echo "[diagnostics] started_at=$(date -Is)"
echo "[diagnostics] app_root=$APP_ROOT"
echo "[diagnostics] meta_file=$META_FILE"

if [[ -f "$META_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$META_FILE"
  set +a
else
  echo "[diagnostics] release-meta.env is missing"
fi

COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/compose.prod.yml}"
RUNTIME_ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT_DIR/runtime.env}"
POSTGRES_USER="${POSTGRES_USER:-app}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
POSTGRES_DB="${POSTGRES_DB:-app}"
POSTGRES_CONTAINER_NAME="${POSTGRES_CONTAINER_NAME:-ecs-postgres-1}"
PSQL_TIMEOUT_SECONDS="${PSQL_TIMEOUT_SECONDS:-30}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://127.0.0.1:8000/readyz}"
compose=(docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE")

section "git"
if git -C "$APP_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$APP_ROOT" status -sb 2>&1 | redact
  git -C "$APP_ROOT" log -1 --oneline 2>&1 | redact
else
  echo "[diagnostics] not a git repository: $APP_ROOT"
fi

section "env presence"
for key in \
  APP_ROOT COMPOSE_FILE RUNTIME_ENV_FILE DATABASE_URL POSTGRES_USER POSTGRES_DB \
  AUTH_TOKEN_SECRET ADMIN_BOOTSTRAP_USERNAME ADMIN_BOOTSTRAP_PASSWORD \
  WECOM_ENV_PROFILES WECOM_COMPANY_A_CORP_ID WECOM_COMPANY_A_APP_SECRET \
  SMARTSHEET_COMPANY_A_ID WEDOC_COMPANY_A_DOCID \
  WECOM_COMPANY_B_CORP_ID WECOM_COMPANY_B_APP_SECRET \
  SMARTSHEET_COMPANY_B_ID WEDOC_COMPANY_B_DOCID; do
  print_env_presence "$key"
done | redact

section "database url parse"
python3 - <<'PY' 2>&1 | redact
import os
from urllib.parse import urlparse
url = os.environ.get("DATABASE_URL", "")
p = urlparse(url)
print("scheme=", repr(p.scheme))
print("host=", repr(p.hostname))
print("db=", repr((p.path or "").lstrip("/")))
print("user_set=", bool(p.username))
print("password_len=", len(p.password or ""))
PY

safe_run "compose services" "${compose[@]}" config --services
safe_run "compose ps" "${compose[@]}" ps
safe_run "backend health" curl -fsS "$HEALTHCHECK_URL"
safe_run "public-web" curl -fsS http://127.0.0.1:8080
safe_run "admin-ui" curl -fsS http://127.0.0.1:8081

section "doc-sync tables"
for table in external_sources external_fields external_records sync_runs sync_requests; do
  PGPASSWORD="$POSTGRES_PASSWORD" timeout "$PSQL_TIMEOUT_SECONDS" docker exec -e PGPASSWORD "$POSTGRES_CONTAINER_NAME" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "select '${table}=' || coalesce((select count(*)::text from public.${table}), 'missing')" 2>&1 | redact \
    || echo "$table=CHECK_FAILED"
done

section "recent sync runs"
PGPASSWORD="$POSTGRES_PASSWORD" timeout "$PSQL_TIMEOUT_SECONDS" docker exec -e PGPASSWORD "$POSTGRES_CONTAINER_NAME" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "select id || ' provider=' || provider || ' profile=' || coalesce(env_profile,'') || ' status=' || status || ' records=' || record_count || ' errors=' || error_count || ' started=' || started_at from sync_runs order by id desc limit 5" 2>&1 | redact \
  || echo "[diagnostics] sync_runs query failed"

section "recent backend logs"
"${compose[@]}" logs --tail=120 backend-api 2>&1 | redact || echo "[diagnostics] backend logs unavailable"

section "recent doc-sync logs"
"${compose[@]}" logs --tail=120 doc-sync-worker 2>&1 | redact || echo "[diagnostics] doc-sync logs unavailable"
"${compose[@]}" logs --tail=120 tplus-write-worker 2>&1 | redact || echo "[diagnostics] tplus-write logs unavailable"

section "recent postgres logs"
"${compose[@]}" logs --tail=80 postgres 2>&1 | redact || echo "[diagnostics] postgres logs unavailable"

echo
echo "[diagnostics] finished_at=$(date -Is)"
