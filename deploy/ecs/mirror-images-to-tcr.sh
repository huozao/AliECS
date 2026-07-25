#!/usr/bin/env bash
set -euo pipefail

: "${TCR_BASE:?example: ccr.ccs.tencentyun.com/namespace}"
: "${TCR_USERNAME:?TCR push username}"
: "${TCR_PASSWORD:?TCR push password}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="${1:-$ROOT_DIR/third-party-images.lock}"
command -v skopeo >/dev/null || {
  echo "skopeo is required" >&2
  exit 1
}

registry_host="${TCR_BASE%%/*}"
auth_file="$(mktemp)"
trap 'rm -f "$auth_file"' EXIT
skopeo login --authfile "$auth_file" --username "$TCR_USERNAME" --password-stdin \
  "$registry_host" <<<"$TCR_PASSWORD"

mirror_one() {
  local name="$1" source="$2" tag="$3" destination source_digest target_digest
  destination="docker://${TCR_BASE}/third-party-${name}:${tag}"
  echo "[mirror] $source -> ${destination#docker://}"
  skopeo copy --all --preserve-digests --authfile "$auth_file" \
    "docker://$source" "$destination"
  source_digest="${source##*@}"
  target_digest="$(skopeo inspect --authfile "$auth_file" "$destination" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Digest"])')"
  [[ "$target_digest" == "$source_digest" ]] || {
    echo "[mirror] digest mismatch: source=$source_digest target=$target_digest" >&2
    exit 1
  }
}

while IFS='|' read -r name source tag; do
  [[ -n "$name" && "${name:0:1}" != "#" ]] || continue
  [[ "$source" == *@sha256:* ]] || {
    echo "[mirror] source must be digest-pinned: $source" >&2
    exit 1
  }
  mirror_one "$name" "$source" "$tag"
done < "$LOCK_FILE"
