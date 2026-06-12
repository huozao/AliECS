# Couple Immich Handoff

## Current Phase

Phase 7 full verification completed with external blockers recorded.

## Completed Work

- Recorded AliECS git status.
- Recorded webdock git status.
- Confirmed SSH reachability for `aliecs`.
- Confirmed SSH reachability for `webdock`.
- Checked Docker availability on both remote hosts.
- Checked disk space, running Docker containers, and common Immich paths on old laptop.
- Detected no existing Immich path at `~/immich-app`, `/opt/immich`, or `/srv/immich`.
- Created `~/immich-app` on `webdock`.
- Downloaded official Immich `docker-compose.yml` and `example.env` release assets.
- Configured remote Immich `.env` with local library/postgres paths, `IMMICH_VERSION=v2`, and `TZ=Asia/Shanghai`.
- Randomized remote `DB_PASSWORD` without printing it.
- Started Immich with Docker Compose.
- Verified local Immich ping.
- Confirmed all Immich containers are healthy.
- Added `docs/ops/couple-immich-runbook.md`.
- Inspected ECS Nginx layout at `/etc/nginx/conf.d/aliecs.conf`.
- Verified ECS could not reach Immich on `127.0.0.1:2283`, `host.docker.internal:2283`, or `100.97.176.57:2283`.
- Confirmed old laptop Immich listens on `0.0.0.0:2283` and responds locally.
- Confirmed existing WebDock reverse tunnel is `127.0.0.1:11800 -> 100.97.176.57:18000`.
- Backed up ECS `/root/.ssh/authorized_keys`.
- Extended the existing `webdock-ecs-tunnel` SSH key to allow `permitlisten="127.0.0.1:12283"`.
- Verified `sshd -t` after authorized_keys change.
- Added old laptop `webdock-immich-tunnel.service`.
- Verified ECS `127.0.0.1:12283/api/server/ping` returns `{"res":"pong"}`.
- Added ECS `/etc/nginx/conf.d/immich.conf` HTTP proxy for `immich.hydwang.xyz`.
- Verified `nginx -t` and reloaded Nginx.
- Verified ECS local Host-header proxy: `curl -H "Host: immich.hydwang.xyz" http://127.0.0.1/api/server/ping` returns `{"res":"pong"}`.
- Confirmed `immich.hydwang.xyz` DNS currently does not resolve, so public HTTPS cannot be completed automatically.
- Added `services/backend-api/app/immich_client.py`.
- Added `/v1/immich/status` route guarded by `couple_memory_access`.
- Added Immich runtime config placeholders to ECS env examples and compose.
- Documented Immich env variables in `docs/env-matrix.md`.
- Added `tests/test_couple_immich_client.py`.
- Verified ImmichClient tests and full AliECS unittest suite.
- Added `db/migrations/0004_couple_immich_assets.sql`.
- Added `ImmichAssetBindRequest`.
- Added `POST /v1/memories/{memory_id}/immich-assets`.
- Added `GET /v1/memories/{memory_id}/immich-assets`.
- Added `DELETE /v1/memories/{memory_id}/immich-assets/{binding_id}`.
- Added `tests/test_couple_immich_assets.py`.
- Verified binding tests and full AliECS unittest suite.
- Updated shared memory API to include `immich_assets`.
- Updated Memory detail page to list and manually bind Immich asset references.
- Updated Couple dashboard to show Immich status.
- Updated share page to render only Immich assets bound to the shared memory.
- Verified full AliECS unittest suite after UI/API updates.
- Added WebDock Immich maintenance scripts:
  - `deploy/laptop/immich/check-immich-health.sh`
  - `deploy/laptop/immich/backup-immich-preflight.sh`
  - `deploy/laptop/immich/update-immich-safe.sh`
- Set Git executable bits for the three WebDock Immich maintenance scripts.
- Installed the three maintenance scripts to `webdock:~/immich-app/`.
- Verified remote script syntax with `bash -n`.
- Verified `check-immich-health.sh` succeeds against the running Immich service.
- Verified `backup-immich-preflight.sh` fails closed because no recent Immich database backup exists under `./library/backups`.
- Verified full AliECS unittest suite after maintenance updates.
- Verified ECS production Compose config renders with `deploy/ecs/runtime.env.example`.
- Verified old laptop Immich containers are healthy and local ping returns `{"res":"pong"}`.
- Verified ECS Immich reverse tunnel and Nginx Host-header proxy return `{"res":"pong"}`.
- Verified sensitive paths are not staged; only WebDock Immich maintenance scripts are staged.

## Read-Only Audit Results

### AliECS working tree

Existing dirty state was present before the Immich long-run execution. Do not revert unrelated files.

```text
 M deploy/ecs/compose.prod.yml
 M deploy/ecs/deploy.sh
 M deploy/ecs/release-meta.env.example
 M deploy/ecs/runtime.env.example
 M deploy/openclaw-bridge/openclaw_bridge.py
 M docs/env-matrix.md
 M docs/webdock-openclaw-integration.md
 M services/backend-api/app/main.py
 M tests/test_openclaw_bridge.py
?? docs/ops/
?? docs/superpowers/plans/2026-06-11-feishu-openclaw-auto-reply.md
?? docs/superpowers/plans/2026-06-12-couple-immich-long-run.md
?? docs/superpowers/specs/
?? tests/test_couple_webdock_photo_storage.py
```

### webdock working tree

Existing dirty state was present before the Immich long-run execution. Do not revert unrelated files.

```text
 M README.md
 M deploy/laptop/.env.example
 M deploy/laptop/compose.yml
 M docs/operations.md
 M requirements.txt
 M src/api/routes_chat.py
 M src/config.py
 M src/main.py
 M tests/test_openai_chat_completions.py
?? src/api/routes_storage.py
?? tests/test_photo_storage.py
```

### Remote hosts

- `aliecs` responded: host `iZrj9bybtxzjn7kf2bw6ltZ`, Docker `29.5.2`.
- `webdock` responded: host `webdock-laptop`, Docker `29.1.3`.
- Old laptop root filesystem: `/dev/sda4`, 366G total, 27G used, 321G available.
- Old laptop running Docker containers: `webdock` healthy, ports `100.97.176.57:6080->6080` and `100.97.176.57:18000->8000`.
- Common Immich paths checked: `~/immich-app`, `/opt/immich`. No existing directory was printed by the read-only audit command.

## Verification

Commands run:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS status --short
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\webdock status --short
ssh aliecs "hostname; date; docker --version || true"
ssh webdock "hostname; date; docker --version || true"
ssh webdock "set -eu; df -h; docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true; test -d ~/immich-app && ls -la ~/immich-app || true; test -d /opt/immich && ls -la /opt/immich || true"
ssh webdock 'set -eu; for d in ~/immich-app /opt/immich /srv/immich; do if [ -e "$d" ]; then echo FOUND:$d; ls -la "$d"; fi; done'
ssh webdock "set -eu; mkdir -p ~/immich-app; cd ~/immich-app; test -f docker-compose.yml || wget -O docker-compose.yml https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml; test -f .env || wget -O .env https://github.com/immich-app/immich/releases/latest/download/example.env; chmod 600 .env"
ssh webdock 'set -eu; cd ~/immich-app; cp .env .env.pre-codex.$(date +%Y%m%d%H%M%S); python3 - <<PY
from pathlib import Path
import secrets
import string
p = Path(".env")
lines = p.read_text().splitlines()
seen = set()
out = []
updates = {"UPLOAD_LOCATION": "./library", "DB_DATA_LOCATION": "./postgres", "TZ": "Asia/Shanghai", "IMMICH_VERSION": "v2"}
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
        if key == "DB_PASSWORD" and line.strip() == "DB_PASSWORD=postgres":
            chars = string.ascii_letters + string.digits
            pwd = "".join(secrets.choice(chars) for _ in range(32))
            out.append("DB_PASSWORD=" + pwd)
            continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
p.write_text("\n".join(out) + "\n")
PY
grep -E "^(UPLOAD_LOCATION|DB_DATA_LOCATION|TZ|IMMICH_VERSION)=" .env'
ssh webdock "set -eu; cd ~/immich-app; docker compose up -d; docker compose ps"
ssh webdock 'set -eu; cd ~/immich-app; for i in $(seq 1 30); do if curl -fsS http://127.0.0.1:2283/api/server/ping; then exit 0; fi; sleep 2; done; docker compose logs --tail=120 immich-server; exit 1'
ssh webdock "cd ~/immich-app && docker compose ps"
ssh aliecs "set -eu; nginx -T 2>/tmp/nginx-all.txt || true; grep -R \"server_name .*hydwang\" -n /etc/nginx /etc/nginx/conf.d /etc/nginx/sites-enabled 2>/dev/null || true"
ssh aliecs "set -eu; curl -fsS http://127.0.0.1:2283/api/server/ping || curl -fsS http://host.docker.internal:2283/api/server/ping || true"
ssh webdock "hostname -I; ip -brief addr | sed -n '1,20p'; ss -ltnp | grep 2283 || true"
ssh aliecs "curl -fsS --connect-timeout 5 http://100.97.176.57:2283/api/server/ping || true"
ssh aliecs "systemctl list-units --type=service --all | grep -i webdock || true; ss -ltnp | grep -E '11800|2283' || true; ps aux | grep -E 'ssh.*11800|ssh.*2283|socat|webdock-tunnel' | grep -v grep || true"
ssh webdock "systemctl --user list-units --type=service --all | grep -i tunnel || true; systemctl list-units --type=service --all | grep -i tunnel || true; ps aux | grep -E 'ssh.*11800|ssh.*2283|autossh|tunnel' | grep -v grep || true"
ssh aliecs 'set -eu; sudo cp /root/.ssh/authorized_keys /root/.ssh/authorized_keys.bak.immich-$(date +%Y%m%d%H%M%S); sudo python3 - <<PY
from pathlib import Path
p = Path("/root/.ssh/authorized_keys")
text = p.read_text()
old = "permitlisten=\"127.0.0.1:11800\""
new = "permitlisten=\"127.0.0.1:11800\",permitlisten=\"127.0.0.1:12283\""
lines = []
changed = False
for line in text.splitlines():
    if "webdock-ecs-tunnel" in line and old in line and "127.0.0.1:12283" not in line:
        line = line.replace(old, new)
        changed = True
    lines.append(line)
if not changed and "127.0.0.1:12283" not in text:
    raise SystemExit("webdock tunnel authorized_keys line not updated")
p.write_text("\n".join(lines) + "\n")
PY
sudo sshd -t
sudo grep -n "webdock-ecs-tunnel\|12283" /root/.ssh/authorized_keys'
ssh webdock 'set -eu; sudo tee /etc/systemd/system/webdock-immich-tunnel.service >/dev/null <<EOF
[Unit]
Description=Immich reverse tunnel to ECS
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=webdock
ExecStart=/usr/bin/ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=yes -i /home/webdock/.ssh/webdock_ecs_tunnel_ed25519 -R 127.0.0.1:12283:100.97.176.57:2283 root@47.77.176.62
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now webdock-immich-tunnel.service
sudo systemctl --no-pager --full status webdock-immich-tunnel.service | sed -n "1,12p"'
ssh aliecs "set -eu; ss -ltnp | grep 12283 || true; curl -fsS http://127.0.0.1:12283/api/server/ping"
ssh webdock "systemctl is-active webdock-ecs-tunnel.service; systemctl is-active webdock-immich-tunnel.service; ps aux | grep -E 'ssh.*11800|ssh.*12283' | grep -v grep"
ssh aliecs 'set -eu; sudo tee /etc/nginx/conf.d/immich.conf >/dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name immich.hydwang.xyz;

    client_max_body_size 50000M;
    proxy_request_buffering off;
    proxy_buffering off;

    location / {
        proxy_pass http://127.0.0.1:12283;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF
sudo nginx -t
sudo systemctl reload nginx'
ssh aliecs "curl -fsS -H 'Host: immich.hydwang.xyz' http://127.0.0.1/api/server/ping; curl -fsS https://immich.hydwang.xyz/api/server/ping || true"
ssh aliecs "sudo nginx -t"
python -m unittest discover -s tests -p test_couple_immich_client.py -v
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p test_couple_immich_assets.py -v
python -m unittest discover -s tests -v
python -m unittest discover -s tests -v
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\webdock add --chmod=+x deploy/laptop/immich/check-immich-health.sh deploy/laptop/immich/backup-immich-preflight.sh deploy/laptop/immich/update-immich-safe.sh
scp deploy/laptop/immich/check-immich-health.sh deploy/laptop/immich/backup-immich-preflight.sh deploy/laptop/immich/update-immich-safe.sh webdock:~/immich-app/
ssh webdock "set -eu; chmod +x ~/immich-app/check-immich-health.sh ~/immich-app/backup-immich-preflight.sh ~/immich-app/update-immich-safe.sh; bash -n ~/immich-app/check-immich-health.sh ~/immich-app/backup-immich-preflight.sh ~/immich-app/update-immich-safe.sh; IMMICH_DIR=~/immich-app ~/immich-app/check-immich-health.sh"
ssh webdock "set +e; IMMICH_DIR=~/immich-app ~/immich-app/backup-immich-preflight.sh 2>&1; code=$?; echo EXIT:$code; exit 0"
python -m unittest discover -s tests -v
docker compose --env-file deploy/ecs/runtime.env.example -f deploy/ecs/compose.prod.yml config
ssh webdock "set -eu; cd ~/immich-app && docker compose ps && curl -fsS http://127.0.0.1:2283/api/server/ping"
ssh aliecs "set -eu; curl -fsS http://127.0.0.1:12283/api/server/ping; curl -fsS -H 'Host: immich.hydwang.xyz' http://127.0.0.1/api/server/ping; curl -fsS --connect-timeout 8 https://immich.hydwang.xyz/api/server/ping || true"
git status --short -- .env logs browser_data _references
git diff --cached --name-only
```

## Blockers

- Public DNS for `immich.hydwang.xyz` does not resolve yet. HTTPS certificate issuance and public HTTPS smoke are pending.
- Immich first-admin setup and API key creation still require human login.
- Safe Immich update is intentionally blocked until a recent Immich database backup exists under `~/immich-app/library/backups`. Current backup directory only has the `.immich` marker.

## Next Step

Manual external tasks remain:

1. Add DNS for `immich.hydwang.xyz` to the ECS public IP.
2. Issue or expand TLS certificate for `immich.hydwang.xyz`, then convert the Nginx route to HTTPS.
3. Complete Immich first-admin setup and create an API key.
4. Put the real `IMMICH_API_KEY` only in runtime secrets / `.env`, never git.
5. Create or verify a real Immich database backup under `~/immich-app/library/backups` before running `update-immich-safe.sh`.
