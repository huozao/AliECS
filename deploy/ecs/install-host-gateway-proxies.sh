#!/usr/bin/env bash
set -euo pipefail

# Exposes host-only business helpers to Docker through host.docker.internal.
# WebDock photo storage on 11800 keeps its legacy unit for rollback compatibility.

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
DESTDIR="${DESTDIR:-}"
INSTALL_ROOT="${DESTDIR%/}"

install -d -m 0755 "$INSTALL_ROOT/opt/aliecs"
install -d -m 0755 "$INSTALL_ROOT/etc/default"
install -d -m 0755 "$INSTALL_ROOT/etc/systemd/system"
install -m 0644 "$ROOT_DIR/webdock-tunnel-proxy.py" \
  "$INSTALL_ROOT/opt/aliecs/webdock-tunnel-proxy.py"
install -m 0644 "$ROOT_DIR/host-gateway-proxy@.service" \
  "$INSTALL_ROOT/etc/systemd/system/host-gateway-proxy@.service"

render_instance() {
  local name="$1"
  local port="$2"
  cat > "$INSTALL_ROOT/etc/default/host-gateway-proxy-$name" <<ENV
WEBDOCK_PROXY_BIND_HOST=172.17.0.1
WEBDOCK_PROXY_BIND_PORT=$port
WEBDOCK_PROXY_TARGET_HOST=127.0.0.1
WEBDOCK_PROXY_TARGET_PORT=$port
WEBDOCK_PROXY_BACKLOG=64
ENV
}

render_instance wecom-kf 18080
render_instance erpnext 18200
render_instance paperless 18201

if [[ -n "$DESTDIR" ]]; then
  exit 0
fi

systemctl daemon-reload

# Claude's 2026-08-21 emergency repair temporarily used this template for
# WebDock photos too. Hand 11800 back to the established rollback-compatible
# webdock-tunnel-proxy.service before that service is restarted by deploy.sh.
systemctl disable --now host-gateway-proxy@webdock-photo.service \
  >/dev/null 2>&1 || true

for instance in wecom-kf erpnext paperless; do
  systemctl enable "host-gateway-proxy@$instance.service"
  systemctl restart "host-gateway-proxy@$instance.service"
  systemctl is-active --quiet "host-gateway-proxy@$instance.service"
done
