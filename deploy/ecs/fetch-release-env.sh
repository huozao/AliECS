#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

SECRET_NAME="${ALIYUN_KMS_SECRET_NAME:-}"
REGION_ID="${ALIYUN_REGION_ID:-cn-hangzhou}"
TARGET_FILE="${ALIYUN_RELEASE_META_TARGET:-$ROOT_DIR/release-meta.env}"
TMP_FILE="${TARGET_FILE}.tmp.$$"
JSON_FILE="${TARGET_FILE}.kms.$$"

cleanup() {
  rm -f "$TMP_FILE" "$JSON_FILE"
}
trap cleanup EXIT

if [[ -z "$SECRET_NAME" ]]; then
  if [[ -f "$TARGET_FILE" ]]; then
    echo "[env-sync] ALIYUN_KMS_SECRET_NAME is not set; keeping existing env file: ${TARGET_FILE}"
    exit 0
  fi

  echo "[env-sync] ALIYUN_KMS_SECRET_NAME is not set and ${TARGET_FILE} is missing." >&2
  echo "[env-sync] Set ALIYUN_KMS_SECRET_NAME after KMS is ready, or create release-meta.env on ECS." >&2
  exit 1
fi

if ! command -v aliyun >/dev/null 2>&1; then
  echo "[env-sync] Missing aliyun CLI. Install Alibaba Cloud CLI on ECS, or unset ALIYUN_KMS_SECRET_NAME to keep local release-meta.env." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[env-sync] Missing python3. It is required to decode the KMS payload." >&2
  exit 1
fi

mkdir -p "$(dirname "$TARGET_FILE")"

echo "[env-sync] Fetching release env from KMS secret: ${SECRET_NAME}"
aliyun kms GetSecretValue \
  --RegionId "$REGION_ID" \
  --SecretName "$SECRET_NAME" \
  --output json > "$JSON_FILE"

python3 - "$TMP_FILE" "$JSON_FILE" <<'PY'
import base64
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
json_file = pathlib.Path(sys.argv[2])
payload = json.loads(json_file.read_text(encoding="utf-8"))
secret_data = payload.get("SecretData", "")

try:
    decoded = base64.b64decode(secret_data, validate=True)
except Exception as exc:
    raise SystemExit(f"[env-sync] SecretData is not valid base64: {exc}")

required = [
    b"APP_ROOT=",
    b"COMPOSE_FILE=",
    b"RUNTIME_ENV_FILE=",
    b"DATABASE_URL=",
    b"AUTH_TOKEN_SECRET=",
]
missing = [name.decode("ascii").rstrip("=") for name in required if name not in decoded]
if missing:
    raise SystemExit("[env-sync] Decoded env is missing required keys: " + ", ".join(missing))

target.write_bytes(decoded)
PY

rm -f "$JSON_FILE"
chmod 600 "$TMP_FILE"
mv "$TMP_FILE" "$TARGET_FILE"

echo "[env-sync] Wrote ECS private env file: ${TARGET_FILE}"
