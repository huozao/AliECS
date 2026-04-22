#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

MIGRATIONS_DIR="${MIGRATIONS_DIR:-/opt/app/db/migrations}"

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

echo "[迁移] 执行 $MIGRATIONS_DIR 下的 SQL"
shopt -s nullglob
sql_files=("$MIGRATIONS_DIR"/*.sql)
if (( ${#sql_files[@]} == 0 )); then
  echo "[迁移] 无 SQL 文件，跳过"
  exit 0
fi

for sql in "${sql_files[@]}"; do
  echo "[迁移] 应用 $sql"
  docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < "$sql"
done

echo "[迁移] 完成"
