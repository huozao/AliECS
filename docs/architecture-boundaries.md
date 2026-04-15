# Architecture Boundaries (AI-First)

## Runtime ownership
- Host-native (not containerized here): Nginx, Certbot, sing-box, deploy script runtime wrapper.
- Compose-managed: public-web, admin-ui, backend-api, postgres.

## Facts of truth
- GitHub repository = source code truth.
- GHCR = image truth.
- ECS host = runtime node, not source truth.
- Codex = collaboration workspace, not auto-deploy trigger.

## Mandatory constraints
- Do not containerize Nginx.
- Do not containerize sing-box.
- Deploy flow must run migration before service switch.
- GitHub Actions orchestrates delivery only; runtime ownership stays on ECS.
