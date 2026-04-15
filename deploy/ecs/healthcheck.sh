#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

for ((i=1; i<=HEALTHCHECK_RETRIES; i++)); do
  if curl -fsS "$HEALTHCHECK_URL" >/dev/null; then
    echo "[healthcheck] healthy"
    exit 0
  fi

  echo "[healthcheck] waiting ($i/$HEALTHCHECK_RETRIES)"
  sleep "$HEALTHCHECK_INTERVAL_SECONDS"
done

echo "[healthcheck] failed" >&2
exit 1
