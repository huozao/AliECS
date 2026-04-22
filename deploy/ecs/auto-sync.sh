#!/usr/bin/env bash
set -euo pipefail

BRANCH="${1:-main}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_DIR/local/docker-compose.local.yml}"
LOG_DIR="${LOG_DIR:-$REPO_DIR/deploy/ecs/logs}"
LOG_FILE="${LOG_DIR}/auto-sync.log"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "[$(date -Is)] [auto-sync] start repo=$REPO_DIR branch=$BRANCH"

cd "$REPO_DIR"

echo "[$(date -Is)] [auto-sync] git fetch"
git fetch origin "$BRANCH"

echo "[$(date -Is)] [auto-sync] checkout $BRANCH"
git checkout "$BRANCH"

echo "[$(date -Is)] [auto-sync] hard reset to origin/$BRANCH"
git reset --hard "origin/$BRANCH"

echo "[$(date -Is)] [auto-sync] compose up"
docker compose -f "$COMPOSE_FILE" up --build -d

echo "[$(date -Is)] [auto-sync] compose ps"
docker compose -f "$COMPOSE_FILE" ps

echo "[$(date -Is)] [auto-sync] healthcheck"
curl -fsS http://127.0.0.1:8000/healthz

echo "[$(date -Is)] [auto-sync] success"
