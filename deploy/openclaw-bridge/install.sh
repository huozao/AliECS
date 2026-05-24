#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/openclaw-bridge/install.sh" >&2
  exit 1
fi

install -d -m 0755 /opt/openclaw-bridge
install -m 0755 deploy/openclaw-bridge/openclaw_bridge.py /opt/openclaw-bridge/openclaw_bridge.py
install -m 0644 deploy/openclaw-bridge/openclaw-bridge.service /etc/systemd/system/openclaw-bridge.service

if [[ ! -f /opt/openclaw-bridge/webdock.env ]]; then
  install -m 0600 deploy/openclaw-bridge/webdock.env.example /opt/openclaw-bridge/webdock.env
  echo "Created /opt/openclaw-bridge/webdock.env. Edit WEB_DOCK_API_TOKEN before starting."
fi

python3 -m py_compile /opt/openclaw-bridge/openclaw_bridge.py
systemctl daemon-reload
systemctl enable --now openclaw-bridge.service
