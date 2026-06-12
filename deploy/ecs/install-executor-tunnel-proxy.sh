#!/usr/bin/env bash
set -euo pipefail

# Installs a second instance of the shared reverse-tunnel proxy so that the
# bridge-network mcp-coding-server container can reach the 开发机 coding-executor.
#
# Data path:
#   mcp-coding-server (container) -> host.docker.internal:18091 (172.17.0.1)
#   -> this proxy -> 127.0.0.1:18091 (开发机 reverse SSH tunnel) -> executor.
#
# Reuses /opt/aliecs/webdock-tunnel-proxy.py (installed by
# install-webdock-tunnel-proxy.sh); only the systemd instance and its env differ.

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

: "${EXECUTOR_TUNNEL_PROXY_BIND_HOST:=172.17.0.1}"
: "${EXECUTOR_TUNNEL_PROXY_BIND_PORT:=18091}"
: "${EXECUTOR_TUNNEL_PROXY_TARGET_HOST:=127.0.0.1}"
: "${EXECUTOR_TUNNEL_PROXY_TARGET_PORT:=18091}"
: "${EXECUTOR_TUNNEL_PROXY_BACKLOG:=64}"

install -d -m 0755 /opt/aliecs
install -d -m 0755 /etc/default
install -m 0644 "$ROOT_DIR/webdock-tunnel-proxy.py" /opt/aliecs/webdock-tunnel-proxy.py
install -m 0644 "$ROOT_DIR/executor-tunnel-proxy.service" /etc/systemd/system/executor-tunnel-proxy.service

cat > /etc/default/executor-tunnel-proxy <<ENV
WEBDOCK_PROXY_BIND_HOST=${EXECUTOR_TUNNEL_PROXY_BIND_HOST}
WEBDOCK_PROXY_BIND_PORT=${EXECUTOR_TUNNEL_PROXY_BIND_PORT}
WEBDOCK_PROXY_TARGET_HOST=${EXECUTOR_TUNNEL_PROXY_TARGET_HOST}
WEBDOCK_PROXY_TARGET_PORT=${EXECUTOR_TUNNEL_PROXY_TARGET_PORT}
WEBDOCK_PROXY_BACKLOG=${EXECUTOR_TUNNEL_PROXY_BACKLOG}
ENV

systemctl daemon-reload
systemctl enable executor-tunnel-proxy.service
systemctl restart executor-tunnel-proxy.service
systemctl --no-pager --full status executor-tunnel-proxy.service | sed -n '1,8p'
