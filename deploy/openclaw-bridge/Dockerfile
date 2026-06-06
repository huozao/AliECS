# openclaw-bridge: OpenAI-compatible shim between OpenClaw and webdock.
# It is stdlib-only (http.server + urllib), so there is no pip install step.
# Runtime config (WEB_DOCK_*, OPENCLAW_BRIDGE_HOSTS/PORT) is supplied via the
# env_file at deploy time (see infra/server/compose.bridge.yml). Run with
# network_mode: host so 127.0.0.1:18080 and the 127.0.0.1:11800 reverse tunnel
# keep the same semantics as the previous systemd service.
FROM python:3.12-slim

WORKDIR /app
COPY openclaw_bridge.py ./openclaw_bridge.py

EXPOSE 18080
CMD ["python", "-u", "openclaw_bridge.py"]
