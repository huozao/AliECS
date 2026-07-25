#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUSINESS="$ROOT_DIR/compose.business-cn.yml"
EDGE="$ROOT_DIR/compose.edge-us.yml"
COLD="$ROOT_DIR/compose.business-cold-recovery.yml"
ROLE_DEPLOY="$ROOT_DIR/deploy-role.sh"
EDGE_DEPLOY="$ROOT_DIR/deploy-edge.sh"
MIGRATE="$ROOT_DIR/migrate.sh"
RELEASE_WORKFLOW="$ROOT_DIR/../../.github/workflows/release-deploy.yml"
BRIDGE_WORKFLOW="$ROOT_DIR/../../.github/workflows/bridge-cutover.yml"

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
assert_contains "$RELEASE_WORKFLOW" 'TCR_USERNAME: ${{ secrets.TCR_USERNAME }}'
assert_contains "$RELEASE_WORKFLOW" 'TCR_PASSWORD: ${{ secrets.TCR_PASSWORD }}'
assert_not_contains "$RELEASE_WORKFLOW" "TCR_PUSH_USERNAME"
assert_not_contains "$RELEASE_WORKFLOW" "TCR_PULL_USERNAME"
assert_not_contains "$RELEASE_WORKFLOW" "ALIYUN_KMS"
assert_not_contains "$RELEASE_WORKFLOW" "git pull"
assert_contains "$BRIDGE_WORKFLOW" 'host: ${{ secrets.TENCENT_HOST }}'
assert_contains "$BRIDGE_WORKFLOW" "inputs.confirmation == 'CUTOVER_TXECS'"

echo "deploy role boundary tests passed"
