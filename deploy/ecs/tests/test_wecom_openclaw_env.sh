#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_SH="$ROOT_DIR/deploy.sh"
COMPOSE_YML="$ROOT_DIR/compose.prod.yml"

grep -q '^OPENCLAW_INTERNAL_TOKEN="${OPENCLAW_INTERNAL_TOKEN:-}"' "$DEPLOY_SH"
grep -q '^OPENCLAW_INTERNAL_TOKEN=${OPENCLAW_INTERNAL_TOKEN}' "$DEPLOY_SH"
grep -q 'OPENCLAW_INTERNAL_TOKEN: ${OPENCLAW_INTERNAL_TOKEN:-}' "$COMPOSE_YML"
grep -q 'wecom_group_media:/app/wecom-group-media' "$COMPOSE_YML"

echo "OK"
