#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

PREVIOUS_ENV="$METADATA_DIR/previous.env"
if [[ ! -f "$PREVIOUS_ENV" ]]; then
  echo "[回滚] 未找到上一版本元信息，无法回滚" >&2
  exit 1
fi

cp "$PREVIOUS_ENV" "$RUNTIME_ENV_FILE"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d

if "$ROOT_DIR/healthcheck.sh"; then
  echo "[回滚] 已切回上一版本并通过健康检查"
  exit 0
fi

echo "[回滚] 切回上一版本后健康检查仍失败" >&2
exit 1
