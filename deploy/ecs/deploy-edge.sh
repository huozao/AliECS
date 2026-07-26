#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <image-tag>" >&2
  exit 1
fi

IMAGE_TAG="$1"
if [[ ! "$IMAGE_TAG" =~ ^(sha-[0-9a-f]{12,40}|v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?|V[0-9]{11})$ ]]; then
  echo "[deploy] invalid image tag: $IMAGE_TAG" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
META_FILE="${RELEASE_META_FILE:-$ROOT_DIR/release-meta.env}"
[[ "${DEPLOY_ROLE:-}" == "edge-us" ]] || {
  echo "[deploy] deploy-edge.sh requires DEPLOY_ROLE=edge-us" >&2
  exit 1
}
[[ -f "$META_FILE" ]] || {
  echo "[deploy] missing role env: $META_FILE" >&2
  exit 1
}

# shellcheck disable=SC1090
set -a
source "$META_FILE"
set +a

IMAGE_REGISTRY_BASE="${ROLE_IMAGE_REGISTRY_BASE:-${IMAGE_REGISTRY_BASE:-${GHCR_BASE:-}}}"
: "${IMAGE_REGISTRY_BASE:?set IMAGE_REGISTRY_BASE or GHCR_BASE}"
COMPOSE_FILE="${ROLE_COMPOSE_FILE:-${COMPOSE_FILE:-$ROOT_DIR/compose.edge-us.yml}}"
RUNTIME_ENV_FILE="${ROLE_RUNTIME_ENV_FILE:-${RUNTIME_ENV_FILE:-$ROOT_DIR/runtime.edge-us.env}}"
METADATA_DIR="${ROLE_METADATA_DIR:-${METADATA_DIR:-$ROOT_DIR/.release-meta-edge}}"
MCP_CODING_SERVER_TAG="${MCP_CODING_SERVER_TAG:-$IMAGE_TAG}"

if [[ -n "${DEPLOY_COMMIT_SHA:-}" ]]; then
  actual_commit=""
  if git -C "$ROOT_DIR/../.." rev-parse HEAD >/dev/null 2>&1; then
    actual_commit="$(git -C "$ROOT_DIR/../.." rev-parse HEAD)"
  elif [[ -f "$ROOT_DIR/../../.source-commit" ]]; then
    actual_commit="$(tr -d '[:space:]' < "$ROOT_DIR/../../.source-commit")"
  fi
  [[ "$actual_commit" == "$DEPLOY_COMMIT_SHA" ]] || {
    echo "[deploy] source commit mismatch: expected=$DEPLOY_COMMIT_SHA actual=${actual_commit:-missing}" >&2
    exit 1
  }
fi

mkdir -p "$METADATA_DIR" "$(dirname "$RUNTIME_ENV_FILE")"
cat > "$RUNTIME_ENV_FILE" <<ENV
MCP_CODING_SERVER_IMAGE=${IMAGE_REGISTRY_BASE}/mcp-coding-server:${MCP_CODING_SERVER_TAG}
EXECUTOR_BASE_URL=${EXECUTOR_BASE_URL:-}
EXECUTOR_TOKEN=${EXECUTOR_TOKEN:-}
EXECUTOR_TIMEOUT_SECONDS=${EXECUTOR_TIMEOUT_SECONDS:-20}
MCP_OAUTH_ENABLED=${MCP_OAUTH_ENABLED:-false}
MCP_OAUTH_ISSUER=${MCP_OAUTH_ISSUER:-}
MCP_OAUTH_PASSPHRASE=${MCP_OAUTH_PASSPHRASE:-}
MCP_OAUTH_SIGNING_SECRET=${MCP_OAUTH_SIGNING_SECRET:-}
MCP_OAUTH_STORE_PATH=${MCP_OAUTH_STORE_PATH:-/data/oauth/oauth.db}
MCP_OAUTH_ACCESS_TTL=${MCP_OAUTH_ACCESS_TTL:-3600}
MCP_OAUTH_REFRESH_TTL=${MCP_OAUTH_REFRESH_TTL:-2592000}
MCP_OAUTH_CODE_TTL=${MCP_OAUTH_CODE_TTL:-600}
ENV
chmod 0600 "$RUNTIME_ENV_FILE"

mapfile -t services < <(docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" config --services)
if [[ "${services[*]}" != "mcp-coding-server" ]]; then
  echo "[deploy] edge-us compose contains unexpected services: ${services[*]}" >&2
  exit 1
fi

if [[ -n "${REGISTRY_USERNAME:-}" && -n "${REGISTRY_TOKEN:-}" ]]; then
  registry_host="${IMAGE_REGISTRY_BASE%%/*}"
  echo "$REGISTRY_TOKEN" | docker login "$registry_host" -u "$REGISTRY_USERNAME" --password-stdin
fi

docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" pull mcp-coding-server
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d mcp-coding-server

for _ in $(seq 1 12); do
  if curl -fsS --max-time 5 http://127.0.0.1:8090/healthz >/dev/null; then
    printf '%s\n' "${DEPLOY_COMMIT_SHA:-unknown}" > "$METADATA_DIR/last-success-commit"
    echo "[deploy] edge-us healthy"
    exit 0
  fi
  sleep 5
done

echo "[deploy] edge-us health check failed" >&2
exit 1
