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
MANIFEST="$ROOT_DIR/write-deployment-manifest.sh"
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
assert_contains "$MANIFEST" 'META_FILE="${RELEASE_META_FILE:-$ROOT_DIR/release-meta.env}"'
assert_contains "$DEPLOY" 'DEPLOY_SERVICES=(postgres backend-api public-web admin-ui)'
assert_contains "$DEPLOY" 'P0 只限制“启动哪些服务”，不限制预拉镜像'
assert_contains "$DEPLOY" 'DEPLOY_START_SERVICES'
assert_contains "$DEPLOY" 'DEPLOY_START_SERVICES 含非法服务'
assert_contains "$DEPLOY" 'DEPLOY_PREPARE_ONLY'
assert_contains "$DEPLOY" '候选预部署必须且只能设置 DEPLOY_START_SERVICES=postgres'
assert_contains "$DEPLOY" 'PUBLIC_TRAFFIC_SWITCHED=false'
assert_contains "$DEPLOY" 'stop doc-sync-worker tplus-sync-worker tplus-write-worker'
assert_contains "$DEPLOY" '生产 worker 未运行'
assert_contains "$DEPLOY" 'ps --status running --services'
assert_contains "$DEPLOY" 'export DOCKER_CONFIG="$REGISTRY_AUTH_DIR"'
assert_contains "$DEPLOY" 'ALLOW_OFFLINE_CACHED_IMAGES'
assert_contains "$DEPLOY" 'config --images | sort -u'
assert_contains "$DEPLOY" 'docker image inspect "$image"'
assert_contains "$DEPLOY" '离线缓存缺镜像'
assert_contains "$DEPLOY" 'CURRENT_SOURCE_COMMIT="${DEPLOY_COMMIT_SHA:-}"'

assert_contains "$COLD" 'profiles: ["cold-recovery"]'
if ENABLE_COLD_RECOVERY=false bash "$ROLE_DEPLOY" business-cold-recovery sha-0123456789ab >/dev/null 2>&1; then
  echo "cold recovery must be blocked by default" >&2
  exit 1
fi

assert_contains "$RELEASE_WORKFLOW" "  deploy-business-cn:"
assert_contains "$RELEASE_WORKFLOW" "  prepare-business-candidate:"
assert_contains "$RELEASE_WORKFLOW" "  preload-sso-candidate:"
assert_contains "$RELEASE_WORKFLOW" "inputs.deploy_target == 'sso-candidate'"
assert_contains "$RELEASE_WORKFLOW" 'third-party-authelia:4.39-runtime-20260725'
assert_contains "$RELEASE_WORKFLOW" 'third-party-lldap:v0.6-runtime-20260725'
assert_contains "$RELEASE_WORKFLOW" "  deploy-edge-us:"
assert_contains "$RELEASE_WORKFLOW" 'host: ${{ secrets.TENCENT_HOST }}'
assert_contains "$RELEASE_WORKFLOW" "inputs.deploy_target == 'business-cn'"
assert_contains "$RELEASE_WORKFLOW" "inputs.deploy_target == 'business-candidate'"
assert_contains "$RELEASE_WORKFLOW" "inputs.deploy_target == 'edge-us'"
assert_contains "$RELEASE_WORKFLOW" "inputs.deploy_target == 'mirror-only'"
assert_contains "$RELEASE_WORKFLOW" 'ROLE_MIGRATIONS_DIR="$CURRENT_LINK/db/migrations"'
assert_contains "$RELEASE_WORKFLOW" 'ROLE_POSTGRES_CONTAINER_NAME=business-cn-postgres-1'
assert_contains "$RELEASE_WORKFLOW" 'ROLE_HEALTHCHECK_URL=http://127.0.0.1:8080/health/'
assert_contains "$RELEASE_WORKFLOW" 'TXECS_RUNTIME_PROFILE=production'
assert_contains "$RELEASE_WORKFLOW" 'TXECS_PRODUCTION_CONFIRM=render-production-runtime'
assert_contains "$RELEASE_WORKFLOW" "grep -Fxq 'P0_MODE=false'"
assert_contains "$RELEASE_WORKFLOW" "grep -Fxq 'TPLUS_BOM_WRITE_ENABLED=true'"
assert_contains "$RELEASE_WORKFLOW" 'DEPLOY_PREPARE_ONLY=true'
assert_contains "$RELEASE_WORKFLOW" 'DEPLOY_START_SERVICES=postgres'
assert_contains "$RELEASE_WORKFLOW" '/usr/local/sbin/business-candidate-preflight'
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
# TCR 手工入口必须保留，用于 peer 主路径故障时回滚、重切或切非 main 的 ref。
assert_contains "$BRIDGE_WORKFLOW" "  workflow_dispatch:"
assert_contains "$BRIDGE_WORKFLOW" "  workflow_call:"

# bridge 与业务镜像共用 peer 物理通道，但必须保持独立发布、服务和 ACK。
assert_contains "$RELEASE_WORKFLOW" "  stage-openclaw-bridge-peer:"
assert_contains "$RELEASE_WORKFLOW" '"release_type":"openclaw-bridge"'
assert_contains "$RELEASE_WORKFLOW" 'data.get("release_type") != "openclaw-bridge"'
assert_contains "$RELEASE_WORKFLOW" "inputs.deploy_target == 'bridge-peer'"
assert_contains "$RELEASE_WORKFLOW" "needs.resolve-release.outputs.bridge_changed == 'true'"
assert_contains "$RELEASE_WORKFLOW" 'CURRENT="$(git rev-parse "HEAD:deploy/openclaw-bridge")"'
assert_not_contains "$RELEASE_WORKFLOW" "  cutover-bridge:"
assert_contains "$RELEASE_WORKFLOW" '"release_type":"business-cn"'
assert_contains "$BRIDGE_WORKFLOW" \
  'STATE_FILE=/srv/internal-stack/release.env'
assert_contains "$BRIDGE_WORKFLOW" \
  'COMPOSE=/srv/openclaw-bridge/docker-compose.yml'
assert_contains "$BRIDGE_WORKFLOW" \
  'sudo systemctl restart "$SERVICE"'
assert_not_contains "$BRIDGE_WORKFLOW" \
  '/srv/business-cn/state/bridge-release.env'
assert_not_contains "$BRIDGE_WORKFLOW" \
  '/home/ubuntu/infra/server/compose.bridge.yml'

echo "deploy role boundary tests passed"
