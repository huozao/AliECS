# Project Navigation (Humans + AI)

## 1) Main flows
1. Development and delivery:
   local edit -> local compose verify -> push GitHub -> Actions build/push -> SSH trigger ECS deploy script.
2. Runtime serving:
   internet -> host Nginx -> public-web/admin-ui/backend-api -> PostgreSQL.
3. Proxy serving:
   proxy client -> host sing-box -> internet.
4. Future sync (skeleton):
   scheduler -> worker -> fetch -> validate -> upsert -> writeback.

## 2) Where to change by requirement
- Public display pages: `services/public-web/`
- Admin placeholder pages: `services/admin-ui/`
- API endpoint and behavior: `services/backend-api/app/`
- Database bootstrap/migration SQL: `db/migrations/`
- Local topology and startup order: `local/docker-compose.local.yml`
- CI delivery orchestration: `.github/workflows/`
- ECS deployment behavior: `deploy/ecs/`
- Future sync skeleton pipeline: `sync-pipeline/`

## 3) Critical explicit entries
- Local run entry: `local/docker-compose.local.yml`
- API entry: `services/backend-api/app/main.py`
- Migration entry: `deploy/ecs/migrate.sh`
- Deploy entry: `deploy/ecs/deploy.sh`
- Healthcheck entry: `deploy/ecs/healthcheck.sh`
- Rollback entry: `deploy/ecs/rollback.sh`

## 4) Do-not-break boundaries
- Keep Nginx and sing-box host-native (do not containerize here).
- Keep Actions as delivery orchestrator only, not runtime owner.
- Keep deploy order: migration must happen before service switch.
- Keep business logic out of generic `utils/common/shared` dumping folders.
