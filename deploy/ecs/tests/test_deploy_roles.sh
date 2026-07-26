#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUSINESS="$ROOT_DIR/compose.business-cn.yml"
EDGE="$ROOT_DIR/compose.edge-us.yml"
COLD="$ROOT_DIR/compose.business-cold-recovery.yml"
ROLE_DEPLOY="$ROOT_DIR/deploy-role.sh"
EDGE_DEPLOY="$ROOT_DIR/deploy-edge.sh"
MIGRATE="$ROOT_DIR/migrate.sh"
DEPLOY="$ROOT_DIR/deploy.sh"
HEALTHCHECK="$ROOT_DIR/healthcheck.sh"
RELEASE_WORKFLOW="$ROOT_DIR/../../.github/workflows/release-deploy.yml"
BRIDGE_WORKFLOW="$ROOT_DIR/../../.github/workflows/bridge-cutover.yml"
MIRROR_IMAGES="$ROOT_DIR/mirror-images-to-tcr.sh"

assert_contains() {
  local file="$1" pattern="$2"
  grep -Fq -- "$pattern" "$file" || {
    echo "missing in $file: $pattern" >&2
    exit 1
  }
}

assert_not_contains() {
  local file="$1" pattern="$2"
  if grep -Fq -- "$pattern" "$file"; then
    echo "unexpected in $file: $pattern" >&2
    exit 1
  fi
}

for service in public-web admin-ui backend-api doc-sync-worker tplus-sync-worker tplus-write-worker postgres; do
  assert_contains "$BUSINESS" "  $service:"
done
assert_not_contains "$BUSINESS" "mcp-coding-server:"
assert_not_contains "$BUSINESS" "sing-box"
assert_not_contains "$BUSINESS" "console"

assert_contains "$EDGE" "  mcp-coding-server:"
assert_not_contains "$EDGE" "  postgres:"
assert_not_contains "$EDGE_DEPLOY" "migrate.sh"
assert_contains "$MIGRATE" 'business-cn|legacy-all'
assert_contains "$MIGRATE" '无权执行数据库迁移'
assert_contains "$MIGRATE" 'META_FILE="${RELEASE_META_FILE:-$ROOT_DIR/release-meta.env}"'
assert_contains "$MIGRATE" 'ROLE_MIGRATIONS_DIR'
assert_contains "$MIGRATE" 'ROLE_POSTGRES_CONTAINER_NAME'
assert_contains "$HEALTHCHECK" 'META_FILE="${RELEASE_META_FILE:-$ROOT_DIR/release-meta.env}"'
assert_contains "$HEALTHCHECK" 'ROLE_HEALTHCHECK_URL'
assert_contains "$DEPLOY" 'DEPLOY_SERVICES=(postgres backend-api public-web admin-ui)'
assert_contains "$DEPLOY" 'stop doc-sync-worker tplus-sync-worker tplus-write-worker'

assert_contains "$COLD" 'profiles: ["cold-recovery"]'
if ENABLE_COLD_RECOVERY=false bash "$ROLE_DEPLOY" business-cold-recovery sha-0123456789ab >/dev/null 2>&1; then
  echo "cold recovery must be blocked by default" >&2
  exit 1
fi

assert_contains "$RELEASE_WORKFLOW" "  deploy-business-cn:"
assert_contains "$RELEASE_WORKFLOW" "  deploy-edge-us:"
assert_contains "$RELEASE_WORKFLOW" 'host: ${{ secrets.TENCENT_HOST }}'
assert_contains "$RELEASE_WORKFLOW" "inputs.deploy_target == 'business-cn'"
assert_contains "$RELEASE_WORKFLOW" "inputs.deploy_target == 'edge-us'"
assert_contains "$RELEASE_WORKFLOW" "inputs.deploy_target == 'mirror-only'"
assert_contains "$RELEASE_WORKFLOW" 'ROLE_MIGRATIONS_DIR="$CURRENT_LINK/db/migrations"'
assert_contains "$RELEASE_WORKFLOW" 'ROLE_POSTGRES_CONTAINER_NAME=business-cn-postgres-1'
assert_contains "$RELEASE_WORKFLOW" 'ROLE_HEALTHCHECK_URL=http://127.0.0.1:8080/health/'
assert_contains "$RELEASE_WORKFLOW" 'TCR_USERNAME: ${{ secrets.TCR_USERNAME }}'
assert_contains "$RELEASE_WORKFLOW" 'TCR_PASSWORD: ${{ secrets.TCR_PASSWORD }}'
assert_not_contains "$RELEASE_WORKFLOW" "TCR_PUSH_USERNAME"
assert_not_contains "$RELEASE_WORKFLOW" "TCR_PULL_USERNAME"
assert_not_contains "$RELEASE_WORKFLOW" "ALIYUN_KMS"
assert_not_contains "$RELEASE_WORKFLOW" "git pull"
assert_contains "$RELEASE_WORKFLOW" 'auth_dir="$(mktemp -d)"'
assert_contains "$RELEASE_WORKFLOW" 'auth_file="$auth_dir/auth.json"'
assert_not_contains "$RELEASE_WORKFLOW" 'auth_file="$(mktemp)"'
assert_contains "$MIRROR_IMAGES" 'auth_dir="$(mktemp -d)"'
assert_contains "$MIRROR_IMAGES" 'auth_file="$auth_dir/auth.json"'
assert_not_contains "$MIRROR_IMAGES" 'auth_file="$(mktemp)"'
assert_contains "$BRIDGE_WORKFLOW" 'host: ${{ secrets.TENCENT_HOST }}'
assert_contains "$BRIDGE_WORKFLOW" "inputs.confirmation == 'CUTOVER_TXECS'"

echo "deploy role boundary tests passed"
