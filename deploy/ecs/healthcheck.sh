#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

for ((i=1; i<=HEALTHCHECK_RETRIES; i++)); do
  if curl -fsS "$HEALTHCHECK_URL" >/dev/null; then
    echo "[健康检查] 通过"
    exit 0
  fi

  echo "[健康检查] 等待中（$i/$HEALTHCHECK_RETRIES）"
  sleep "$HEALTHCHECK_INTERVAL_SECONDS"
done

echo "[健康检查] 失败：$HEALTHCHECK_URL" >&2
exit 1
