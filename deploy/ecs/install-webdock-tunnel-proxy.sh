#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

: "${WEBDOCK_TUNNEL_PROXY_BIND_HOST:=172.17.0.1}"
: "${WEBDOCK_TUNNEL_PROXY_BIND_PORT:=11800}"
: "${WEBDOCK_TUNNEL_PROXY_TARGET_HOST:=127.0.0.1}"
: "${WEBDOCK_TUNNEL_PROXY_TARGET_PORT:=11800}"
: "${WEBDOCK_TUNNEL_PROXY_BACKLOG:=64}"

install -d -m 0755 /opt/aliecs
install -d -m 0755 /etc/default
install -m 0644 "$ROOT_DIR/webdock-tunnel-proxy.py" /opt/aliecs/webdock-tunnel-proxy.py
install -m 0644 "$ROOT_DIR/webdock-tunnel-proxy.service" /etc/systemd/system/webdock-tunnel-proxy.service

cat > /etc/default/webdock-tunnel-proxy <<ENV
WEBDOCK_PROXY_BIND_HOST=${WEBDOCK_TUNNEL_PROXY_BIND_HOST}
WEBDOCK_PROXY_BIND_PORT=${WEBDOCK_TUNNEL_PROXY_BIND_PORT}
WEBDOCK_PROXY_TARGET_HOST=${WEBDOCK_TUNNEL_PROXY_TARGET_HOST}
WEBDOCK_PROXY_TARGET_PORT=${WEBDOCK_TUNNEL_PROXY_TARGET_PORT}
WEBDOCK_PROXY_BACKLOG=${WEBDOCK_TUNNEL_PROXY_BACKLOG}
ENV

systemctl daemon-reload
systemctl enable webdock-tunnel-proxy.service
systemctl restart webdock-tunnel-proxy.service
systemctl --no-pager --full status webdock-tunnel-proxy.service | sed -n '1,8p'
