#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_SH="$ROOT_DIR/deploy.sh"
COMPOSE_YML="$ROOT_DIR/compose.prod.yml"

required_names=(
  FEISHU_SESSION_CONSOLE_APP_TOKEN
  FEISHU_SESSION_CONSOLE_MESSAGE_TABLE_ID
  FEISHU_SESSION_CONSOLE_TASK_TABLE_ID
  FEISHU_SESSION_CONSOLE_SESSION_TABLE_ID
  FEISHU_SESSION_CONSOLE_CHATGPT_PROJECT_URL
  FEISHU_SESSION_CONSOLE_CHATGPT_PROJECT_NAME
  FEISHU_COMPANY_A_SESSION_CONSOLE_APP_TOKEN
  FEISHU_COMPANY_A_SESSION_CONSOLE_MESSAGE_TABLE_ID
  FEISHU_COMPANY_A_SESSION_CONSOLE_TASK_TABLE_ID
  FEISHU_COMPANY_A_SESSION_CONSOLE_SESSION_TABLE_ID
  FEISHU_COMPANY_A_SESSION_CONSOLE_CHATGPT_PROJECT_URL
  FEISHU_COMPANY_A_SESSION_CONSOLE_CHATGPT_PROJECT_NAME
)

for name in "${required_names[@]}"; do
  if ! grep -q "^${name}=\"\\\${${name}:-" "$DEPLOY_SH"; then
    echo "deploy.sh must initialize ${name}" >&2
    exit 1
  fi

  if ! grep -q "^${name}=\\\${${name}}" "$DEPLOY_SH"; then
    echo "deploy.sh must write ${name} into runtime env" >&2
    exit 1
  fi

  if ! grep -q "${name}: \\\${${name}:-}" "$COMPOSE_YML"; then
    echo "compose.prod.yml must pass ${name} to Feishu workers" >&2
    exit 1
  fi
done

echo "OK"
