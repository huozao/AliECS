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
auth_dir="$(mktemp -d)"
auth_file="$auth_dir/auth.json"
trap 'rm -rf "$auth_dir"' EXIT
skopeo login --authfile "$auth_file" --username "$TCR_USERNAME" --password-stdin \
  "$registry_host" <<<"$TCR_PASSWORD"

# 两段搬运：先把镜像落到本地目录，再从目录推 TCR。
#
# 为什么需要它：skopeo copy 是**流式**的，读 GHCR 和写 TCR 在同一条管道上。
# 写 TCR 那一跳跨境，慢下来会通过 backpressure 把读端也拖住，直到 GHCR 那边
# 超时断开——日志里看到的是 `Reading blob body from https://ghcr.io/... failed
# (unexpected EOF)`，很容易误判成 GHCR 有问题，而 runner 到 GHCR 本该是最快的一跳。
#
# 2026-09-10 实测：supersync 那两个大 blob（53MB 层）连着两轮 rerun、共 6 次
# 20 分钟尝试全部这样超时。既有四个第三方镜像没踩到，是因为它们的层更碎。
# 分成两段后读写解耦：读 GHCR 在 runner 上很快，写 TCR 慢但不会再拖累读端。
#
# 默认关闭，因为它多占一份磁盘、也多一次落盘 IO；只给会卡的镜像开。
MIRROR_VIA_DIR="${MIRROR_VIA_DIR:-false}"

copy_with_retry() { # src dst label
  local src="$1" dst="$2" label="$3" attempt
  for attempt in 1 2 3; do
    if timeout 20m skopeo copy --all --preserve-digests --retry-times 3 \
      --authfile "$auth_file" "$src" "$dst"; then
      return 0
    fi
    if [[ "$attempt" == 3 ]]; then
      echo "[mirror] $label failed after 3 attempts" >&2
      return 1
    fi
    echo "[mirror] $label attempt $attempt failed or timed out; retrying in 20s" >&2
    sleep 20
  done
}

mirror_one() {
  local name="$1" source="$2" tag="$3" destination source_digest target_digest
  destination="docker://${TCR_BASE}/third-party-${name}:${tag}"
  echo "[mirror] $source -> ${destination#docker://}"
  if [[ "$MIRROR_VIA_DIR" == "true" ]]; then
    local stage
    stage="$(mktemp -d)"
    echo "[mirror] two-stage via $stage"
    copy_with_retry "docker://$source" "dir:$stage" "$name(pull)" || { rm -rf "$stage"; exit 1; }
    copy_with_retry "dir:$stage" "$destination" "$name(push)" || { rm -rf "$stage"; exit 1; }
    rm -rf "$stage"
    source_digest="${source##*@}"
    target_digest="$(skopeo inspect --authfile "$auth_file" "$destination" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Digest"])')"
    [[ "$target_digest" == "$source_digest" ]] || {
      echo "[mirror] digest mismatch: source=$source_digest target=$target_digest" >&2
      exit 1
    }
    return 0
  fi
  # 与 release-deploy 的 TCR 同步同一跳跨境，同样会挂起而非报错，故超时+重试并用。
  local attempt
  for attempt in 1 2 3; do
    if timeout 20m skopeo copy --all --preserve-digests --retry-times 3 \
      --authfile "$auth_file" "docker://$source" "$destination"; then
      break
    fi
    if [[ "$attempt" == 3 ]]; then
      echo "[mirror] failed after 3 attempts: $name" >&2
      exit 1
    fi
    echo "[mirror] attempt $attempt failed or timed out; retrying in 20s" >&2
    sleep 20
  done
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
