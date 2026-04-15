# Architecture Boundaries (AI-First)

## Runtime ownership
- Host-native: Nginx, Certbot, sing-box, ECS deploy trigger/runtime wrapper.
- Compose-managed: public-web, admin-ui, backend-api, postgres.

## Facts of truth
- GitHub repository is source code truth.
- GHCR is container image truth.
- ECS is runtime node, not source truth.
- Codex is collaboration entry, not deploy/source truth.

## Configuration boundary vs code boundary
- Prefer configuration for: image tags, host addresses, DB credentials, workflow secrets.
- Change code for: API behavior, deploy control flow, migration logic, sync pipeline behavior.

## Mandatory constraints
- Do not containerize Nginx.
- Do not containerize sing-box.
- Migration must run before compose service switch.
- Actions orchestrates delivery only; ECS owns runtime execution.
