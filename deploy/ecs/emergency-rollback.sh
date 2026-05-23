#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
META_FILE="$ROOT_DIR/release-meta.env"

if [[ ! -f "$META_FILE" ]]; then
  echo "[emergency-rollback] Missing config file: $META_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$META_FILE"

: "${COMPOSE_FILE:?Please set COMPOSE_FILE in release-meta.env}"
: "${RUNTIME_ENV_FILE:?Please set RUNTIME_ENV_FILE in release-meta.env}"
: "${METADATA_DIR:?Please set METADATA_DIR in release-meta.env}"

PREVIOUS_ENV="$METADATA_DIR/previous.env"
if [[ ! -f "$PREVIOUS_ENV" ]]; then
  echo "[emergency-rollback] No previous release env found: $PREVIOUS_ENV" >&2
  exit 1
fi

echo "[emergency-rollback] Current images:"
if [[ -f "$RUNTIME_ENV_FILE" ]]; then
  grep -E '^(PUBLIC_WEB_IMAGE|ADMIN_UI_IMAGE|BACKEND_API_IMAGE|DOC_SYNC_WORKER_IMAGE)=' "$RUNTIME_ENV_FILE" || true
else
  echo "[emergency-rollback] Current runtime env is missing: $RUNTIME_ENV_FILE"
fi

echo "[emergency-rollback] Previous images:"
grep -E '^(PUBLIC_WEB_IMAGE|ADMIN_UI_IMAGE|BACKEND_API_IMAGE|DOC_SYNC_WORKER_IMAGE)=' "$PREVIOUS_ENV" || true

echo "[emergency-rollback] Switching AliECS compose project to previous runtime env"
cp "$PREVIOUS_ENV" "$RUNTIME_ENV_FILE"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d

echo "[emergency-rollback] Running healthcheck"
"$ROOT_DIR/healthcheck.sh"

echo "[emergency-rollback] Compose status"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" ps

echo "[emergency-rollback] Rollback completed"
