#!/usr/bin/env bash
# deploy/ecs/tests/test_validate_tag.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_SH="$ROOT_DIR/deploy.sh"

assert_rejects() {
  local tag="$1"
  if "$DEPLOY_SH" "$tag" >/dev/null 2>&1; then
    echo "FAIL: expected '$tag' to be rejected" >&2
    exit 1
  fi
}

assert_accepts_format() {
  # deploy.sh exits early on missing release-meta.env, but only AFTER tag
  # validation. A tag-format failure must happen before the meta-file check,
  # so we look for the tag-format error message specifically.
  local tag="$1"
  local output
  output="$("$DEPLOY_SH" "$tag" 2>&1 || true)"
  if echo "$output" | grep -q "镜像标签格式错误"; then
    echo "FAIL: expected '$tag' to pass format validation, got: $output" >&2
    exit 1
  fi
}

assert_rejects "latest"
assert_rejects "v1.2"
assert_rejects "1.2.3"
assert_accepts_format "v1.2.3"
assert_accepts_format "v1.2.3-rc.1"

echo "OK"
