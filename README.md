# AliECS AI-Friendly Project (Phase 1 Implemented)

## Project positioning
- **GitHub**: source code truth.
- **GHCR**: deployable image truth.
- **Alibaba ECS**: runtime node.
- **Codex**: AI collaboration entry (review/propose patch), **not** source/deploy truth.

## Phase 1 delivered scope
- Local runnable stack: `public-web + admin-ui(placeholder) + backend-api + postgres`.
- API health/ready endpoints with real DB ping.
- CI workflow for validation and image build checks.
- Release workflow for build/push three images and SSH-trigger ECS deploy.
- ECS deploy scripts with explicit flow:
  `migrate -> switch services -> healthcheck -> rollback`.
- Future sync pipeline explicit skeleton, not full business implementation.

## Fast start
```bash
docker compose -f local/docker-compose.local.yml up --build
```

Access:
- Public web: http://localhost:8080
- Admin placeholder: http://localhost:8081
- API: http://localhost:8000/healthz

## Main navigation
- Architecture boundaries: `docs/architecture-boundaries.md`
- Modification navigation: `docs/project-navigation.md`
- Local runtime entry: `local/docker-compose.local.yml`
- Deploy entry: `deploy/ecs/deploy.sh`
- Migration entry: `deploy/ecs/migrate.sh`
- Healthcheck entry: `deploy/ecs/healthcheck.sh`
