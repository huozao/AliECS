#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

if [[ ! -f "$RUNTIME_ENV_FILE" ]]; then
  echo "[迁移] 找不到运行时环境文件：$RUNTIME_ENV_FILE" >&2
  exit 1
fi

echo "[迁移] 先确保 postgres 已启动"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d postgres

echo "[迁移] 执行 /opt/app/db/migrations 下的 SQL"
for sql in /opt/app/db/migrations/*.sql; do
  [[ -f "$sql" ]] || continue
  echo "[迁移] 应用 $sql"
  docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < "$sql"
done

echo "[迁移] 完成"
