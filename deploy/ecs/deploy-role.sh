#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <business-cn|edge-us|business-cold-recovery> <image-tag>" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ROLE="$1"
IMAGE_TAG="$2"
export DEPLOY_ROLE

case "$DEPLOY_ROLE" in
  business-cn)
    export COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/compose.business-cn.yml}"
    exec bash "$ROOT_DIR/deploy.sh" "$IMAGE_TAG"
    ;;
  edge-us)
    export COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/compose.edge-us.yml}"
    exec bash "$ROOT_DIR/deploy-edge.sh" "$IMAGE_TAG"
    ;;
  business-cold-recovery)
    if [[ "${ENABLE_COLD_RECOVERY:-false}" != "true" ]]; then
      echo "[deploy] business-cold-recovery is definition-only; set ENABLE_COLD_RECOVERY=true after the single-writer recovery gate." >&2
      exit 1
    fi
    echo "[deploy] cold recovery requires the dedicated restore runbook; generic deploy is intentionally blocked." >&2
    exit 1
    ;;
  *)
    echo "[deploy] unsupported role: $DEPLOY_ROLE" >&2
    exit 1
    ;;
esac
