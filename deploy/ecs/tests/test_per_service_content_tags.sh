#!/usr/bin/env bash
# deploy/ecs/tests/test_per_service_content_tags.sh
# 契约：runtime.env 渲染必须支持按服务的内容寻址标签（*_TAG），未提供时回落 IMAGE_TAG；
# 迁移条件化依赖 last-success-commit 记录。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_SH="$ROOT_DIR/deploy.sh"
WORKFLOW="$ROOT_DIR/../../.github/workflows/release-deploy.yml"

assert_contains() {
  local file="$1" needle="$2"
  if ! grep -qF "$needle" "$file"; then
    echo "FAIL: $file 缺少：$needle" >&2
    exit 1
  fi
}

for svc_var in \
  'PUBLIC_WEB_IMAGE=${GHCR_BASE}/public-web:${PUBLIC_WEB_TAG:-$IMAGE_TAG}' \
  'ADMIN_UI_IMAGE=${GHCR_BASE}/admin-ui:${ADMIN_UI_TAG:-$IMAGE_TAG}' \
  'BACKEND_API_IMAGE=${GHCR_BASE}/backend-api:${BACKEND_API_TAG:-$IMAGE_TAG}' \
  'DOC_SYNC_WORKER_IMAGE=${GHCR_BASE}/doc-sync-worker:${DOC_SYNC_WORKER_TAG:-$IMAGE_TAG}' \
  'TPLUS_SYNC_WORKER_IMAGE=${GHCR_BASE}/tplus-sync-worker:${TPLUS_SYNC_WORKER_TAG:-$IMAGE_TAG}' \
  'MCP_CODING_SERVER_IMAGE=${GHCR_BASE}/mcp-coding-server:${MCP_CODING_SERVER_TAG:-$IMAGE_TAG}'; do
  assert_contains "$DEPLOY_SH" "$svc_var"
done

# 迁移条件化 + 成功后记录 commit
assert_contains "$DEPLOY_SH" 'LAST_SUCCESS_COMMIT_FILE="$METADATA_DIR/last-success-commit"'
assert_contains "$DEPLOY_SH" 'FORCE_MIGRATIONS'
assert_contains "$DEPLOY_SH" 'diff --quiet "$last_success" HEAD -- db deploy/ecs/migrate.sh'

# workflow 侧：内容标签计算、存在即跳过、commit 别名、deploy 导出 *_TAG
assert_contains "$WORKFLOW" 'tag=t-$(git rev-parse "HEAD:${{ matrix.context }}" | cut -c1-12)'
assert_contains "$WORKFLOW" 'docker manifest inspect'
assert_contains "$WORKFLOW" 'docker buildx imagetools create'
assert_contains "$WORKFLOW" 'release_id: ${{ steps.vars.outputs.release_id }}'
assert_contains "$WORKFLOW" 'DEPLOY_RUN_ATTEMPT: ${{ github.run_attempt }}'
assert_contains "$WORKFLOW" 'export PUBLIC_WEB_TAG="$(content_tag services/public-web)"'
assert_contains "$WORKFLOW" 'export MCP_CODING_SERVER_TAG="$(content_tag services/mcp-coding-server)"'

bash -n "$DEPLOY_SH"

echo "PASS: per-service content tags + conditional migrations"
