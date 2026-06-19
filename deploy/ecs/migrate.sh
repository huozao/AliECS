#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
META_FILE="$ROOT_DIR/release-meta.env"
if [[ ! -f "$META_FILE" ]]; then
  echo "[迁移] 找不到配置文件：$META_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$META_FILE"

: "${COMPOSE_FILE:?请在 release-meta.env 设置 COMPOSE_FILE}"
: "${RUNTIME_ENV_FILE:?请在 release-meta.env 设置 RUNTIME_ENV_FILE}"
: "${POSTGRES_USER:?请在 release-meta.env 设置 POSTGRES_USER}"
: "${POSTGRES_PASSWORD:?请在 release-meta.env 设置 POSTGRES_PASSWORD}"
: "${POSTGRES_DB:?请在 release-meta.env 设置 POSTGRES_DB}"

MIGRATIONS_DIR="${MIGRATIONS_DIR:-/root/AliECS/db/migrations}"
PSQL_TIMEOUT_SECONDS="${PSQL_TIMEOUT_SECONDS:-30}"

if [[ ! -f "$RUNTIME_ENV_FILE" ]]; then
  echo "[迁移] 找不到运行时环境文件：$RUNTIME_ENV_FILE" >&2
  exit 1
fi

if [[ ! -d "$MIGRATIONS_DIR" ]]; then
  echo "[迁移] 找不到迁移目录：$MIGRATIONS_DIR" >&2
  exit 1
fi

db_host="${DATABASE_URL#*@}"
db_host="${db_host%%/*}"
echo "[迁移] 目标：host=${db_host} db=${POSTGRES_DB} 镜像tag=${IMAGE_TAG:-unset}"

echo "[迁移] 先确保 postgres 已启动"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d postgres

echo "[迁移] 等待 postgres 就绪"
for ((i=1; i<=30; i++)); do
  if docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    echo "[迁移] postgres 已就绪"
    break
  fi

  if (( i == 30 )); then
    echo "[迁移] postgres 未在预期时间内就绪" >&2
    exit 1
  fi

  sleep 2
done

run_psql_file() {
  local sql_file="$1"
  local retries=5
  local delay=2
  for ((attempt=1; attempt<=retries; attempt++)); do
    if PGPASSWORD="$POSTGRES_PASSWORD" timeout "$PSQL_TIMEOUT_SECONDS" docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" exec -T -e PGPASSWORD postgres \
      psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < "$sql_file"; then
      return 0
    fi
    if (( attempt == retries )); then
      return 1
    fi
    echo "[迁移] psql 连接失败，${delay}s 后重试（${attempt}/${retries}）"
    sleep "$delay"
  done
}

run_psql_query() {
  local sql="$1"
  PGPASSWORD="$POSTGRES_PASSWORD" timeout "$PSQL_TIMEOUT_SECONDS" docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" exec -T -e PGPASSWORD postgres \
    psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "$sql"
}

echo "[迁移] 执行 $MIGRATIONS_DIR 下的 SQL"
shopt -s nullglob
sql_files=("$MIGRATIONS_DIR"/*.sql)
if (( ${#sql_files[@]} == 0 )); then
  echo "[迁移] 无 SQL 文件，跳过"
  exit 0
fi

echo "[迁移] 确保 schema_migrations 存在"
run_psql_query "CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW());" >/dev/null

registered_count="$(run_psql_query "SELECT COUNT(*) FROM schema_migrations;")"
has_users_table="$(run_psql_query "SELECT CASE WHEN to_regclass('public.users') IS NULL THEN 'f' ELSE 't' END;")"
if [[ "$registered_count" == "0" && "$has_users_table" == "t" ]]; then
  echo "[迁移] 检测到既有库首次启用登记表，预登记 0009 之前的迁移"
  for sql in "${sql_files[@]}"; do
    version="$(basename "$sql" .sql)"
    if [[ "$version" < "0009_couple_phase3" ]]; then
      run_psql_query "INSERT INTO schema_migrations(version) VALUES ('$version') ON CONFLICT(version) DO NOTHING;" >/dev/null
    fi
  done
fi

for sql in "${sql_files[@]}"; do
  version="$(basename "$sql" .sql)"
  already_applied="$(run_psql_query "SELECT CASE WHEN EXISTS (SELECT 1 FROM schema_migrations WHERE version = '$version') THEN 't' ELSE 'f' END;")"
  if [[ "$already_applied" == "t" ]]; then
    echo "[迁移] 跳过已登记 $version"
    continue
  fi
  echo "[迁移] 应用 $sql"
  run_psql_file "$sql"
  run_psql_query "INSERT INTO schema_migrations(version) VALUES ('$version') ON CONFLICT(version) DO NOTHING;" >/dev/null
done

echo "[迁移] 完成"
