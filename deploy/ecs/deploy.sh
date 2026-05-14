#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "用法：$0 <镜像标签>" >&2
  exit 1
fi

IMAGE_TAG="$1"
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
META_FILE="$ROOT_DIR/release-meta.env"

if [[ ! -f "$META_FILE" ]]; then
  echo "[部署] 缺少配置文件：$META_FILE" >&2
  echo "[部署] 请在 ECS 上创建 release-meta.env，并配置镜像、数据库、登录密钥和管理员账号。" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$META_FILE"

: "${GHCR_BASE:?请在 release-meta.env 设置 GHCR_BASE}"
: "${POSTGRES_USER:?请在 release-meta.env 设置 POSTGRES_USER}"
: "${POSTGRES_PASSWORD:?请在 release-meta.env 设置 POSTGRES_PASSWORD}"
: "${POSTGRES_DB:?请在 release-meta.env 设置 POSTGRES_DB}"
: "${DATABASE_URL:?请在 release-meta.env 设置 DATABASE_URL}"
: "${COMPOSE_FILE:?请在 release-meta.env 设置 COMPOSE_FILE}"
: "${RUNTIME_ENV_FILE:?请在 release-meta.env 设置 RUNTIME_ENV_FILE}"
: "${METADATA_DIR:?请在 release-meta.env 设置 METADATA_DIR}"

: "${AUTH_TOKEN_SECRET:?请在 release-meta.env 设置 AUTH_TOKEN_SECRET}"
: "${ADMIN_BOOTSTRAP_USERNAME:?请在 release-meta.env 设置 ADMIN_BOOTSTRAP_USERNAME}"
: "${ADMIN_BOOTSTRAP_PASSWORD:?请在 release-meta.env 设置 ADMIN_BOOTSTRAP_PASSWORD}"
: "${ADMIN_BOOTSTRAP_DISPLAY_NAME:?请在 release-meta.env 设置 ADMIN_BOOTSTRAP_DISPLAY_NAME}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[部署] 缺少 python3，无法校验 DATABASE_URL。" >&2
  exit 1
fi

db_parse_output="$(python3 - <<'PY'
import os
import sys
from urllib.parse import urlparse

db_url = os.environ.get("DATABASE_URL", "")
parsed = urlparse(db_url)

if parsed.scheme not in {"postgresql", "postgres"}:
    print("invalid_scheme")
    sys.exit(0)

db_name = (parsed.path or "").lstrip("/")
print(parsed.username or "")
print(parsed.password or "")
print(parsed.hostname or "")
print(str(parsed.port or ""))
print(db_name)
PY
)"

db_url_user="$(echo "$db_parse_output" | sed -n '1p')"
db_url_password="$(echo "$db_parse_output" | sed -n '2p')"
db_url_host="$(echo "$db_parse_output" | sed -n '3p')"
db_url_port="$(echo "$db_parse_output" | sed -n '4p')"
db_url_name="$(echo "$db_parse_output" | sed -n '5p')"
db_url_password_len="${#db_url_password}"

if [[ "$db_url_user" == "invalid_scheme" ]]; then
  echo "[部署] DATABASE_URL 协议错误，必须是 postgresql:// 或 postgres://" >&2
  exit 1
fi

if [[ -z "$db_url_user" || -z "$db_url_password" || -z "$db_url_host" || -z "$db_url_name" ]]; then
  echo "[部署] DATABASE_URL 缺少关键字段（用户名/密码/主机/库名）。" >&2
  exit 1
fi

if [[ "$db_url_user" != "$POSTGRES_USER" || "$db_url_password" != "$POSTGRES_PASSWORD" || "$db_url_name" != "$POSTGRES_DB" ]]; then
  echo "[部署] DATABASE_URL 与 POSTGRES_* 不一致，已中止部署。" >&2
  echo "[部署] 解析结果：user=$db_url_user host=$db_url_host port=${db_url_port:-5432} db=$db_url_name pass_len=$db_url_password_len" >&2
  echo "[部署] 环境结果：POSTGRES_USER=$POSTGRES_USER POSTGRES_DB=$POSTGRES_DB POSTGRES_PASSWORD_LEN=${#POSTGRES_PASSWORD}" >&2
  exit 1
fi

AUTH_TOKEN_TTL_SECONDS="${AUTH_TOKEN_TTL_SECONDS:-28800}"
COUPLE_FEATURE_ENABLED="${COUPLE_FEATURE_ENABLED:-false}"
COUPLE_ROUTE="${COUPLE_ROUTE:-/couple/}"
COUPLE_ALLOWED_USERS="${COUPLE_ALLOWED_USERS:-}"
COUPLE_ALLOWED_EMAILS="${COUPLE_ALLOWED_EMAILS:-}"

LOG_DIR="${DEPLOY_LOG_DIR:-$ROOT_DIR/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/deploy-$(date +%Y%m%d).log"
exec > >(tee -a "$LOG_FILE") 2>&1

mkdir -p "$METADATA_DIR"
CURRENT_ENV="$METADATA_DIR/current.env"
PREVIOUS_ENV="$METADATA_DIR/previous.env"

if [[ -f "$CURRENT_ENV" ]]; then
  cp "$CURRENT_ENV" "$PREVIOUS_ENV"
fi

cat > "$CURRENT_ENV" <<ENV
PUBLIC_WEB_IMAGE=${GHCR_BASE}/public-web:${IMAGE_TAG}
ADMIN_UI_IMAGE=${GHCR_BASE}/admin-ui:${IMAGE_TAG}
BACKEND_API_IMAGE=${GHCR_BASE}/backend-api:${IMAGE_TAG}

POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=${POSTGRES_DB}
DATABASE_URL=${DATABASE_URL}

AUTH_TOKEN_SECRET=${AUTH_TOKEN_SECRET}
AUTH_TOKEN_TTL_SECONDS=${AUTH_TOKEN_TTL_SECONDS}
ADMIN_BOOTSTRAP_USERNAME=${ADMIN_BOOTSTRAP_USERNAME}
ADMIN_BOOTSTRAP_PASSWORD=${ADMIN_BOOTSTRAP_PASSWORD}
ADMIN_BOOTSTRAP_DISPLAY_NAME=${ADMIN_BOOTSTRAP_DISPLAY_NAME}
COUPLE_FEATURE_ENABLED=${COUPLE_FEATURE_ENABLED}
COUPLE_ROUTE=${COUPLE_ROUTE}
COUPLE_ALLOWED_USERS=${COUPLE_ALLOWED_USERS}
COUPLE_ALLOWED_EMAILS=${COUPLE_ALLOWED_EMAILS}
ENV

cp "$CURRENT_ENV" "$RUNTIME_ENV_FILE"

if [[ -n "${GHCR_USERNAME:-}" && -n "${GHCR_TOKEN:-}" ]]; then
  echo "[部署] 登录 GHCR"
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
fi

echo "[部署] 拉取镜像"
if ! docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" pull; then
  echo "[部署] 拉取镜像失败。若报 unauthorized，请检查：" >&2
  echo "[部署] 1) GHCR 包可见性（public/private）" >&2
  echo "[部署] 2) GHCR_USERNAME / GHCR_TOKEN 是否在 ECS 或 Actions 中正确提供" >&2
  echo "[部署] 3) GHCR_BASE 与镜像命名是否一致（例如 ghcr.io/huozao/*）" >&2
  exit 1
fi

echo "[部署] 先执行迁移"
"$ROOT_DIR/migrate.sh"

echo "[部署] 再切换服务"
docker compose --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" up -d

if "$ROOT_DIR/healthcheck.sh"; then
  echo "[部署] 成功"
  exit 0
fi

echo "[部署] 失败，开始回滚"
"$ROOT_DIR/rollback.sh"
exit 1
