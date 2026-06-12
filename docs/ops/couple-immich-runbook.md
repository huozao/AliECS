# Couple Immich Runbook

## Service Location

- Host: `webdock`
- Directory: `~/immich-app`
- Public route: `immich.hydwang.xyz` after the reverse proxy phase is complete
- Local health: `http://127.0.0.1:2283/api/server/ping`

## Data Locations

- Uploads: `~/immich-app/library`
- Database data: `~/immich-app/postgres`
- Runtime env: `~/immich-app/.env`
- Compose file: `~/immich-app/docker-compose.yml`

## Safety Rules

- Do not delete or replace `library` or `postgres` without a verified backup and explicit user approval.
- Do not print `DB_PASSWORD`, Immich API keys, or admin credentials.
- Do not commit remote `.env` content.
- Treat Immich database backup and media file backup as separate requirements.
- Serve Immich from a root subdomain such as `immich.hydwang.xyz`, not from `/immich`.

## Basic Operations

Check service health from the old laptop:

```bash
cd ~/immich-app
docker compose ps
curl -fsS http://127.0.0.1:2283/api/server/ping
```

Start or refresh containers:

```bash
cd ~/immich-app
docker compose up -d
```

Read recent server logs:

```bash
cd ~/immich-app
docker compose logs --tail=120 immich-server
```

## Current Initial Deployment

Initial deployment used the official Immich release assets:

- `https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml`
- `https://github.com/immich-app/immich/releases/latest/download/example.env`

Runtime `.env` was adjusted on `webdock`:

- `UPLOAD_LOCATION=./library`
- `DB_DATA_LOCATION=./postgres`
- `IMMICH_VERSION=v2`
- `TZ=Asia/Shanghai`
- `DB_PASSWORD` was randomized and was not printed.

## ECS Reverse Tunnel

Immich uses a separate reverse SSH tunnel from the old laptop to ECS. It does not replace the existing WebDock API tunnel.

- Existing WebDock tunnel: ECS `127.0.0.1:11800` -> old laptop `100.97.176.57:18000`
- Immich tunnel: ECS `127.0.0.1:12283` -> old laptop `100.97.176.57:2283`
- Old laptop service: `webdock-immich-tunnel.service`
- ECS SSH authorized key was extended to allow `permitlisten="127.0.0.1:12283"` for the existing `webdock-ecs-tunnel` key.

Verify from ECS:

```bash
curl -fsS http://127.0.0.1:12283/api/server/ping
```

## ECS Nginx

Current ECS Nginx file:

- `/etc/nginx/conf.d/immich.conf`

The HTTP server block proxies only the new subdomain:

```text
immich.hydwang.xyz -> http://127.0.0.1:12283
```

Verify locally on ECS before DNS is ready:

```bash
curl -fsS -H "Host: immich.hydwang.xyz" http://127.0.0.1/api/server/ping
```

Remaining public access work:

- Add DNS for `immich.hydwang.xyz` pointing to ECS.
- Issue or expand TLS certificate for `immich.hydwang.xyz`.
- Convert the Nginx route to HTTPS after DNS and certificate are ready.

## Immich Maintenance

Health:

```bash
IMMICH_DIR=~/immich-app deploy/laptop/immich/check-immich-health.sh
```

Backup preflight:

```bash
IMMICH_DIR=~/immich-app deploy/laptop/immich/backup-immich-preflight.sh
```

Safe update:

```bash
IMMICH_DIR=~/immich-app deploy/laptop/immich/update-immich-safe.sh
```

The safe update script runs backup preflight first, then `docker compose pull`, `docker compose up -d`, and a health check. It refuses to continue unless a recent Immich database backup exists. Media files still require a separate backup of `UPLOAD_LOCATION`.
