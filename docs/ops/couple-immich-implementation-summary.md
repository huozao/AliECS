# Couple Photo And Immich Implementation Summary

## Scope

This change prepares Couple Memory photo storage for the old laptop and makes Immich the planned long-term photo library.

AliECS owns Couple Memory business data, permissions, share links, and memory-to-asset bindings. WebDock owns the private old-laptop photo storage API used by `STORAGE_DRIVER=webdock`. Immich runs independently on the old laptop from official Immich Docker Compose files and owns original media, thumbnails, mobile backup, search, and future Immich upgrades.

## Runtime Topology

```text
Browser -> AliECS /couple/ -> backend-api
backend-api -> ECS reverse tunnel -> WebDock /storage/photos
backend-api -> Immich API through immich.hydwang.xyz after DNS/TLS/API key are ready
```

Current remote state:

- Immich is installed on `webdock:~/immich-app`.
- ECS can reach Immich through `127.0.0.1:12283`.
- ECS Nginx has an HTTP host route for `immich.hydwang.xyz`.
- Public DNS and TLS for `immich.hydwang.xyz` are still pending.

## AliECS Changes

- Added `webdock` photo storage driver support.
- Added backend proxy endpoint for private WebDock photo content.
- Added Immich API client and `/v1/immich/status`.
- Added `couple_memory_assets` migration for binding memories to Immich assets or albums.
- Added memory asset bind/list/delete endpoints.
- Updated Couple pages to show Immich status, bind asset IDs, and include bound assets in share pages.
- Added runtime env placeholders for WebDock photo storage and Immich integration.

## WebDock Changes

- Added `/storage/photos` API for authenticated upload, read, and delete.
- Added persistent photo storage directory configuration.
- Added Docker volume mapping for host photo storage.
- Added Immich health, backup preflight, and guarded update scripts.

## Required Runtime Variables

AliECS backend:

```text
STORAGE_DRIVER=webdock
WEBDOCK_PHOTO_BASE_URL=http://host.docker.internal:11800
WEBDOCK_PHOTO_API_TOKEN=<runtime secret only>
WEBDOCK_PHOTO_TIMEOUT_SECONDS=30
IMMICH_ENABLED=false
IMMICH_BASE_URL=https://immich.hydwang.xyz
IMMICH_API_KEY=<runtime secret only>
IMMICH_TIMEOUT_SECONDS=20
IMMICH_PROXY_MODE=backend
```

WebDock:

```text
HOST_PHOTO_STORAGE_DIR=/var/lib/webdock/photo_storage
PHOTO_STORAGE_DIR=/app/photo_storage
API_TOKEN=<runtime secret only>
```

## Remaining Manual Work

1. Add DNS for `immich.hydwang.xyz` to the ECS public IP.
2. Issue or expand TLS certificate coverage for `immich.hydwang.xyz`.
3. Complete Immich first-admin setup.
4. Create an Immich API key and store it only in runtime secrets.
5. Create or verify a real Immich database backup before running `update-immich-safe.sh`.

## Verification

Expected verification commands:

```powershell
python -m unittest discover -s tests -v
docker compose --env-file deploy/ecs/runtime.env.example -f deploy/ecs/compose.prod.yml config
ssh webdock "cd ~/immich-app && docker compose ps && curl -fsS http://127.0.0.1:2283/api/server/ping"
ssh aliecs "curl -fsS http://127.0.0.1:12283/api/server/ping"
```

WebDock photo storage:

```powershell
python -m pytest tests/test_photo_storage.py
```
