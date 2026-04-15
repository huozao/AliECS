#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "用法：$0 <镜像标签>" >&2
  exit 1
fi

IMAGE_TAG="$1"
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

mkdir -p "$METADATA_DIR"
CURRENT_ENV="$METADATA_DIR/current.env"
PREVIOUS_ENV="$METADATA_DIR/previous.env"

# 保存上一版本信息，供失败时回滚
if [[ -f "$CURRENT_ENV" ]]; then
  cp "$CURRENT_ENV" "$PREVIOUS_ENV"
fi

# 生成本次发布的运行时镜像引用
cat > "$CURRENT_ENV" <<ENV
PUBLIC_WEB_IMAGE=${GHCR_BASE}/public-web:${IMAGE_TAG}
ADMIN_UI_IMAGE=${GHCR_BASE}/admin-ui:${IMAGE_TAG}
BACKEND_API_IMAGE=${GHCR_BASE}/backend-api:${IMAGE_TAG}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=${POSTGRES_DB}
DATABASE_URL=${DATABASE_URL}
ENV

cp "$CURRENT_ENV" "$RUNTIME_ENV_FILE"

echo "[部署] 拉取镜像"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" pull

echo "[部署] 先执行迁移"
"$ROOT_DIR/migrate.sh"

echo "[部署] 再切换服务"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d

if "$ROOT_DIR/healthcheck.sh"; then
  echo "[部署] 成功"
  exit 0
fi

echo "[部署] 失败，开始回滚"
"$ROOT_DIR/rollback.sh"
exit 1
