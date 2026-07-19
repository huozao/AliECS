#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/meta" "$T/run"
cp "$ROOT_DIR/write-deployment-manifest.sh" "$T/run/"

cat > "$T/run/release-meta.env" <<EOF
METADATA_DIR=$T/meta
RUNTIME_ENV_FILE=$T/runtime.env
EOF

cat > "$T/runtime.env" <<'EOF'
PUBLIC_WEB_IMAGE=ghcr.io/huozao/public-web:t-a
ADMIN_UI_IMAGE=ghcr.io/huozao/admin-ui:t-b
BACKEND_API_IMAGE=ghcr.io/huozao/backend-api:t-c
DOC_SYNC_WORKER_IMAGE=ghcr.io/huozao/doc-sync-worker:t-d
TPLUS_SYNC_WORKER_IMAGE=ghcr.io/huozao/tplus-sync-worker:t-e
MCP_CODING_SERVER_IMAGE=ghcr.io/huozao/mcp-coding-server:t-f
SECRET_VALUE=must-not-leak
EOF

cat > "$T/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf 'ghcr.io/huozao/test@sha256:%064d\n' 0
EOF
chmod +x "$T/bin/docker" "$T/run/write-deployment-manifest.sh"

PATH="$T/bin:$PATH" \
DEPLOY_COMMIT_SHA=0123456789abcdef0123456789abcdef01234567 \
DEPLOY_RUN_ID=12345 \
DEPLOY_RUN_ATTEMPT=2 \
  "$T/run/write-deployment-manifest.sh"

MANIFEST="$T/meta/deployments/0123456789ab-12345-2.json"
test -f "$MANIFEST"
cmp -s "$MANIFEST" "$T/meta/current.json"
python3 - "$MANIFEST" <<'PY'
import json
import pathlib
import sys

payload = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
assert "must-not-leak" not in payload
data = json.loads(payload)
assert data["commit_sha"].startswith("0123456789ab")
assert data["github_run_id"] == "12345"
assert data["github_run_attempt"] == "2"
assert len(data["images"]) == 6
assert all("@sha256:" in image["digest"] for image in data["images"].values())
PY

echo "PASS: deployment manifest is immutable and secret-free"
