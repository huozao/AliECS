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
: "${POSTGRES_DB:?请在 release-meta.env 设置 POSTGRES_DB}"

MIGRATIONS_DIR="${MIGRATIONS_DIR:-/root/AliECS/db/migrations}"

if [[ ! -f "$RUNTIME_ENV_FILE" ]]; then
  echo "[迁移] 找不到运行时环境文件：$RUNTIME_ENV_FILE" >&2
  exit 1
fi

if [[ ! -d "$MIGRATIONS_DIR" ]]; then
  echo "[迁移] 找不到迁移目录：$MIGRATIONS_DIR" >&2
  exit 1
fi

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
    if docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
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

echo "[迁移] 执行 $MIGRATIONS_DIR 下的 SQL"
shopt -s nullglob
sql_files=("$MIGRATIONS_DIR"/*.sql)
if (( ${#sql_files[@]} == 0 )); then
  echo "[迁移] 无 SQL 文件，跳过"
  exit 0
fi

for sql in "${sql_files[@]}"; do
  echo "[迁移] 应用 $sql"
  run_psql_file "$sql"
done

echo "[迁移] 完成"
