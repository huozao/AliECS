#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

PREVIOUS_ENV="$METADATA_DIR/previous.env"
if [[ ! -f "$PREVIOUS_ENV" ]]; then
  echo "[rollback] previous metadata not found, skip rollback" >&2
  exit 1
fi

cp "$PREVIOUS_ENV" "$RUNTIME_ENV_FILE"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d

echo "[rollback] switched to previous release"
