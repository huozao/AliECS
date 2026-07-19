#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/release-meta.env"

: "${METADATA_DIR:?missing METADATA_DIR}"
: "${RUNTIME_ENV_FILE:?missing RUNTIME_ENV_FILE}"

REPO_ROOT="$(cd "$ROOT_DIR/../.." && pwd)"
COMMIT_SHA="${DEPLOY_COMMIT_SHA:-$(git -C "$REPO_ROOT" rev-parse HEAD)}"
RUN_ID="${DEPLOY_RUN_ID:-manual}"
RUN_ATTEMPT="${DEPLOY_RUN_ATTEMPT:-1}"
DEPLOYMENT_ID="${COMMIT_SHA:0:12}-${RUN_ID}-${RUN_ATTEMPT}"
DEPLOYMENTS_DIR="$METADATA_DIR/deployments"

mkdir -p "$DEPLOYMENTS_DIR"

python3 - "$RUNTIME_ENV_FILE" "$DEPLOYMENTS_DIR" "$DEPLOYMENT_ID" "$COMMIT_SHA" "$RUN_ID" "$RUN_ATTEMPT" <<'PY'
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

runtime_path = pathlib.Path(sys.argv[1])
deployments_dir = pathlib.Path(sys.argv[2])
deployment_id, commit_sha, run_id, run_attempt = sys.argv[3:7]
image_keys = (
    "PUBLIC_WEB_IMAGE",
    "ADMIN_UI_IMAGE",
    "BACKEND_API_IMAGE",
    "DOC_SYNC_WORKER_IMAGE",
    "TPLUS_SYNC_WORKER_IMAGE",
    "MCP_CODING_SERVER_IMAGE",
)

values: dict[str, str] = {}
for raw in runtime_path.read_text(encoding="utf-8").splitlines():
    if "=" not in raw or raw.startswith("#"):
        continue
    key, value = raw.split("=", 1)
    if key in image_keys:
        values[key] = value

images: dict[str, dict[str, str]] = {}
for key in image_keys:
    reference = values.get(key, "")
    if not reference:
        raise SystemExit(f"missing image reference in runtime env: {key}")
    digest_ref = reference
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", reference],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip() and result.stdout.strip() != "<no value>":
        digest_ref = result.stdout.strip()
    images[key.removesuffix("_IMAGE").lower().replace("_", "-")] = {
        "env_key": key,
        "reference": reference,
        "digest": digest_ref,
    }

manifest = {
    "schema": 1,
    "deployment_id": deployment_id,
    "commit_sha": commit_sha,
    "github_run_id": run_id,
    "github_run_attempt": run_attempt,
    "deployed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "images": images,
}

payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
history = deployments_dir / f"{deployment_id}.json"
current = deployments_dir.parent / "current.json"
for target in (history, current):
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)
print(f"[deploy-manifest] wrote {history} and {current}")
PY
