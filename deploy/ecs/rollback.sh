#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT_DIR/release-meta.env"

if [[ $# -gt 1 ]]; then
  echo "用法：$0 [deployment_id]" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  PREVIOUS_ENV="$METADATA_DIR/previous.env"
  if [[ ! -f "$PREVIOUS_ENV" ]]; then
    echo "[回滚] 未找到上一版本元信息，无法回滚" >&2
    exit 1
  fi
  cp "$PREVIOUS_ENV" "$RUNTIME_ENV_FILE"
else
  MANIFEST="$METADATA_DIR/deployments/$1.json"
  if [[ ! -f "$MANIFEST" ]]; then
    echo "[回滚] 未找到部署清单：$MANIFEST" >&2
    exit 1
  fi
  python3 - "$MANIFEST" "$RUNTIME_ENV_FILE" <<'PY'
import json
import os
import pathlib
import sys

manifest_path = pathlib.Path(sys.argv[1])
runtime_path = pathlib.Path(sys.argv[2])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
replacements = {
    item["env_key"]: item["digest"]
    for item in manifest.get("images", {}).values()
}
if not replacements:
    raise SystemExit("deployment manifest has no images")

lines = runtime_path.read_text(encoding="utf-8").splitlines()
seen = set()
output = []
for line in lines:
    if "=" in line:
        key = line.split("=", 1)[0]
        if key in replacements:
            line = f"{key}={replacements[key]}"
            seen.add(key)
    output.append(line)
missing = set(replacements) - seen
if missing:
    raise SystemExit(f"runtime env missing image keys: {sorted(missing)}")
tmp = runtime_path.with_suffix(runtime_path.suffix + ".rollback.tmp")
tmp.write_text("\n".join(output) + "\n", encoding="utf-8")
os.replace(tmp, runtime_path)
PY
fi

docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d

if "$ROOT_DIR/healthcheck.sh"; then
  echo "[回滚] 已切回上一版本并通过健康检查"
  exit 0
fi

echo "[回滚] 切回上一版本后健康检查仍失败" >&2
exit 1
