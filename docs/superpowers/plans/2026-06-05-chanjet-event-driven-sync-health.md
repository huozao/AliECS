# Chanjet Event Driven Sync Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Chanjet BOM messages create fast BOM-only sync requests, keep scheduled full sync as reconciliation, and expose operational status in API Health.

**Architecture:** `backend-api` remains the public webhook and health API. `tplus-sync-worker` remains the only component that calls T+ OpenAPI. Webhook requests write durable database rows and return quickly; the worker consumes pending rows asynchronously.

**Tech Stack:** FastAPI, Postgres migrations, Python worker, static public-web HTML/JS, Docker Compose, GitHub Actions deployment.

---

### Task 1: Durable Event And Queue Tables

**Files:**
- Create: `db/migrations/0008_event_driven_sync_health.sql`
- Create: `services/backend-api/app/integrations/events.py`
- Create: `services/backend-api/app/integrations/store.py`
- Test: `tests/test_integration_events.py`
- Test: `tests/test_integration_store.py`

- [x] Add event classification for Chanjet BOM message types.
- [x] Save decoded Chanjet events with stable payload hashes.
- [x] Create `integration_sync_requests` rows for BOM events.
- [x] Keep webhook response fast and successful even if downstream storage fails.

### Task 2: Worker DB Queue Consumption

**Files:**
- Create: `services/tplus-sync-worker/src/tplus_datahub/jobs/db_sync_requests.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/worker_loop.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_bom.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/modules/bom/sync_bom.py`
- Test: `services/tplus-sync-worker/tests/test_db_sync_requests.py`
- Test: `services/tplus-sync-worker/tests/test_worker_loop.py`
- Test: `services/tplus-sync-worker/tests/test_job_sync_bom.py`

- [x] Poll pending BOM DB requests while the worker sleeps between scheduled full syncs.
- [x] Mark DB requests running, then success or failed.
- [x] Support incremental BOM query parameters from `parent_code` and `version`.
- [x] Preserve enabled and disabled BOM scope for targeted sync.

### Task 3: Full Snapshot Reconciliation

**Files:**
- Create: `services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_all.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_bom.py`
- Test: `services/tplus-sync-worker/tests/test_sync_state.py`

- [x] Create stable BOM snapshot hashes.
- [x] Record full BOM snapshots when database config is available.
- [x] Create `needs_review` reconciliation diffs when the latest full snapshot differs from the previous full snapshot.

### Task 4: Ops Health API And UI

**Files:**
- Modify: `services/backend-api/app/main.py`
- Create: `services/public-web/health/index.html`
- Modify: `services/public-web/index.html`
- Test: `tests/test_backend_ops_status.py`

- [x] Keep `/healthz` as lightweight machine health.
- [x] Add `/v1/ops/status` for dashboard data.
- [x] Add `/health/` for human-readable status, queue, diffs, system usage, and configured external host probes.

### Task 5: Deployment Wiring And Docs

**Files:**
- Modify: `deploy/ecs/compose.prod.yml`
- Modify: `local/docker-compose.local.yml`
- Modify: `deploy/ecs/runtime.env.example`
- Modify: `local/.env.local.example`
- Modify: `docs/env-matrix.md`
- Modify: `docs/integration-webhook-gateway.md`
- Modify: `services/tplus-sync-worker/README.md`

- [x] Inject `DATABASE_URL` into `tplus-sync-worker`.
- [x] Add `TPLUS_DB_SYNC_REQUESTS_ENABLED`.
- [x] Document `OPS_HEALTH_HTTP_TARGETS_JSON`.
- [x] Document that webhook-triggered sync is BOM-only and falls back to BOM full sync when target data is missing.
