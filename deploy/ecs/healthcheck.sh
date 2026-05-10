#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
META_FILE="$ROOT_DIR/release-meta.env"
if [[ ! -f "$META_FILE" ]]; then
  echo "[健康检查] 找不到配置文件：$META_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$META_FILE"

: "${HEALTHCHECK_URL:?请在 release-meta.env 设置 HEALTHCHECK_URL}"
: "${HEALTHCHECK_RETRIES:?请在 release-meta.env 设置 HEALTHCHECK_RETRIES}"
: "${HEALTHCHECK_INTERVAL_SECONDS:?请在 release-meta.env 设置 HEALTHCHECK_INTERVAL_SECONDS}"

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
