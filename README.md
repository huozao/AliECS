# AliECS AI-Friendly Project Skeleton (Phase 1)

## What this repository is
This repository is the **source of truth** for an AI-friendly ECS deployment project:
- GitHub: source code truth
- GHCR: image truth
- Alibaba ECS: runtime host
- Local Docker Compose: primary verification environment
- Codex: collaboration entry only (not deploy/source truth)

## Phase 1 goal
Deliver a minimal runnable skeleton:
1. Local run: public-web + backend-api + postgres
2. GitHub Actions: build & push images to GHCR
3. GitHub Actions: trigger ECS host deploy script over SSH
4. Admin UI: placeholder only
5. Future Sync Pipeline: folder + explicit main flow skeleton only

## Quick start (local)
```bash
docker compose -f local/docker-compose.local.yml up --build
```

## Human/AI navigation
- Project map: `docs/project-navigation.md`
- Architecture boundary: `docs/architecture-boundaries.md`
- ECS deploy entry: `deploy/ecs/deploy.sh`
- DB migration entry: `deploy/ecs/migrate.sh`
- Health check entry: `deploy/ecs/healthcheck.sh`
