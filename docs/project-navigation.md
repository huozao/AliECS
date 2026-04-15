# Project Navigation (for Humans + AI)

## Main business threads
1. **Develop & Deliver**: local edit -> local compose verify -> push GitHub -> Actions build/push -> SSH deploy to ECS.
2. **Runtime service**: internet -> host Nginx -> public web / backend API -> Postgres.
3. **Proxy service**: client -> host sing-box -> internet.
4. **Future sync pipeline**: scheduler -> worker -> fetch -> validate -> upsert -> writeback (skeleton only).

## Where to modify by requirement
- Public page behavior: `services/public-web/`
- Admin placeholder: `services/admin-ui/`
- API route/logic: `services/backend-api/app/`
- Schema/migration: `db/migrations/`
- Local startup topology: `local/docker-compose.local.yml`
- CI delivery pipeline: `.github/workflows/`
- ECS rollout process: `deploy/ecs/`
- Future sync flow skeleton: `sync-pipeline/`

## Critical explicit entries
- Local run entry: `local/docker-compose.local.yml`
- API app entry: `services/backend-api/app/main.py`
- DB migration entry: `deploy/ecs/migrate.sh`
- Deploy entry: `deploy/ecs/deploy.sh`
- Health check entry: `deploy/ecs/healthcheck.sh`
