#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

: "${WEBDOCK_FAILOVER_BIND_HOST:=127.0.0.1}"
: "${WEBDOCK_FAILOVER_BIND_PORT:=11800}"
: "${WEBDOCK_FAILOVER_PRIMARY_HOST:=127.0.0.1}"
: "${WEBDOCK_FAILOVER_PRIMARY_PORT:=11810}"
: "${WEBDOCK_FAILOVER_STANDBY_HOST:=127.0.0.1}"
: "${WEBDOCK_FAILOVER_STANDBY_PORT:=11811}"
: "${WEBDOCK_FAILOVER_UPSTREAM_TIMEOUT_SECONDS:=320}"
: "${WEBDOCK_FAILOVER_PRIMARY_DOWN_TTL_SECONDS:=60}"

install -d -m 0755 /opt/aliecs
install -d -m 0755 /etc/default
install -m 0644 "$ROOT_DIR/webdock-failover-proxy.py" /opt/aliecs/webdock-failover-proxy.py
install -m 0644 "$ROOT_DIR/webdock-failover-proxy.service" /etc/systemd/system/webdock-failover-proxy.service

cat > /etc/default/webdock-failover-proxy <<ENV
WEBDOCK_FAILOVER_BIND_HOST=${WEBDOCK_FAILOVER_BIND_HOST}
WEBDOCK_FAILOVER_BIND_PORT=${WEBDOCK_FAILOVER_BIND_PORT}
WEBDOCK_FAILOVER_PRIMARY_HOST=${WEBDOCK_FAILOVER_PRIMARY_HOST}
WEBDOCK_FAILOVER_PRIMARY_PORT=${WEBDOCK_FAILOVER_PRIMARY_PORT}
WEBDOCK_FAILOVER_STANDBY_HOST=${WEBDOCK_FAILOVER_STANDBY_HOST}
WEBDOCK_FAILOVER_STANDBY_PORT=${WEBDOCK_FAILOVER_STANDBY_PORT}
WEBDOCK_FAILOVER_UPSTREAM_TIMEOUT_SECONDS=${WEBDOCK_FAILOVER_UPSTREAM_TIMEOUT_SECONDS}
WEBDOCK_FAILOVER_PRIMARY_DOWN_TTL_SECONDS=${WEBDOCK_FAILOVER_PRIMARY_DOWN_TTL_SECONDS}
ENV

systemctl daemon-reload
systemctl enable webdock-failover-proxy.service
systemctl restart webdock-failover-proxy.service
systemctl --no-pager --full status webdock-failover-proxy.service | sed -n '1,8p'
