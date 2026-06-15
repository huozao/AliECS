#!/usr/bin/env bash
set -euo pipefail

# Makes the ECS-local Immich reverse SSH tunnel (127.0.0.1:12283) reachable from
# Docker containers through host.docker.internal:12283. Reuses the generic
# webdock-tunnel-proxy.py (port-parameterized) under a dedicated systemd unit so
# the Immich and WebDock proxies run side by side.

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

: "${IMMICH_TUNNEL_PROXY_BIND_HOST:=172.17.0.1}"
: "${IMMICH_TUNNEL_PROXY_BIND_PORT:=12283}"
: "${IMMICH_TUNNEL_PROXY_TARGET_HOST:=127.0.0.1}"
: "${IMMICH_TUNNEL_PROXY_TARGET_PORT:=12283}"
: "${IMMICH_TUNNEL_PROXY_BACKLOG:=64}"

install -d -m 0755 /opt/aliecs
install -d -m 0755 /etc/default
install -m 0644 "$ROOT_DIR/webdock-tunnel-proxy.py" /opt/aliecs/webdock-tunnel-proxy.py
install -m 0644 "$ROOT_DIR/immich-tunnel-proxy.service" /etc/systemd/system/immich-tunnel-proxy.service

cat > /etc/default/immich-tunnel-proxy <<ENV
WEBDOCK_PROXY_BIND_HOST=${IMMICH_TUNNEL_PROXY_BIND_HOST}
WEBDOCK_PROXY_BIND_PORT=${IMMICH_TUNNEL_PROXY_BIND_PORT}
WEBDOCK_PROXY_TARGET_HOST=${IMMICH_TUNNEL_PROXY_TARGET_HOST}
WEBDOCK_PROXY_TARGET_PORT=${IMMICH_TUNNEL_PROXY_TARGET_PORT}
WEBDOCK_PROXY_BACKLOG=${IMMICH_TUNNEL_PROXY_BACKLOG}
ENV

systemctl daemon-reload
systemctl enable immich-tunnel-proxy.service
systemctl restart immich-tunnel-proxy.service
systemctl --no-pager --full status immich-tunnel-proxy.service | sed -n '1,8p'
