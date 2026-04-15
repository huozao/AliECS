#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <image-tag>" >&2
  exit 1
fi

IMAGE_TAG="$1"
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

mkdir -p "$METADATA_DIR"
CURRENT_ENV="$METADATA_DIR/current.env"
PREVIOUS_ENV="$METADATA_DIR/previous.env"

if [[ -f "$CURRENT_ENV" ]]; then
  cp "$CURRENT_ENV" "$PREVIOUS_ENV"
fi

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

echo "[deploy] pull images"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" pull

echo "[deploy] migrate before switch"
"$ROOT_DIR/migrate.sh"

echo "[deploy] switch services"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d

if "$ROOT_DIR/healthcheck.sh"; then
  echo "[deploy] success"
  exit 0
fi

echo "[deploy] failed, rollback"
"$ROOT_DIR/rollback.sh"
exit 1
