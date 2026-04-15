#!/usr/bin/env bash
set -euo pipefail

# Explicit deploy flow:
# 1) pull image refs
# 2) run migration
# 3) switch compose services
# 4) health check
# 5) rollback if failed

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

"$ROOT_DIR/migrate.sh"
docker compose -f "$ROOT_DIR/compose.prod.yml" pull

docker compose -f "$ROOT_DIR/compose.prod.yml" up -d

if "$ROOT_DIR/healthcheck.sh"; then
  echo "deploy success"
  exit 0
fi

echo "deploy failed, rollback..."
"$ROOT_DIR/rollback.sh"
exit 1
