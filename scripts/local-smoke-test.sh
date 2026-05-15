#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/local/docker-compose.local.yml"
ENV_FILE="$REPO_ROOT/local/.env.local"
ENV_EXAMPLE="$REPO_ROOT/local/.env.local.example"

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

step() {
  echo
  echo "[local-smoke] $1"
}

show_recent_logs() {
  echo
  echo "[local-smoke] Recent container logs:"
  compose logs --tail=120 || true
}

fail_with_logs() {
  echo
  echo "[local-smoke] FAILED: $1" >&2
  show_recent_logs
  echo
  echo "[local-smoke] To clean up manually, run:"
  echo "docker compose -f local/docker-compose.local.yml down"
  exit 1
}

check_http() {
  local url="$1"
  local required="$2"
  local code

  code="$(curl -fsS -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || true)"
  if [[ "$code" =~ ^[23] ]]; then
    echo "[local-smoke] OK $url -> HTTP $code"
    return 0
  fi

  if [[ "$required" == "required" ]]; then
    fail_with_logs "$url request failed or returned an unexpected status (HTTP ${code:-none})"
  fi

  echo "[local-smoke] Optional check failed: $url is not available (HTTP ${code:-none})."
  return 0
}

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[local-smoke] Missing local/.env.local."
  echo "[local-smoke] Copy the local example and keep only local test values:"
  echo "cp local/.env.local.example local/.env.local"
  echo "[local-smoke] Example file: $ENV_EXAMPLE"
  exit 1
fi

step "Checking Docker Compose config"
compose config

step "Starting local services"
compose up --build -d || fail_with_logs "docker compose up failed"

step "Waiting for backend-api health"
healthy=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 http://localhost:8000/healthz >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done

if [[ "$healthy" != "1" ]]; then
  fail_with_logs "backend-api did not pass /healthz in time"
fi

step "Checking local entrypoints"
check_http "http://localhost:8080" required
check_http "http://localhost:8081" required
check_http "http://localhost:8000/healthz" required
check_http "http://localhost:8000/api/healthz" optional

step "Container status"
compose ps || fail_with_logs "docker compose ps failed"

echo
echo "[local-smoke] Local smoke test finished."
echo "[local-smoke] To clean up manually, run:"
echo "docker compose -f local/docker-compose.local.yml down"
