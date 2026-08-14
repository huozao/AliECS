# Unified Sync Registry Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make production PostgreSQL the private document-locator source of truth, mirror its concise key fields into WeCom A, and move every export/download/copy action into `/sync/` without exposing real external identifiers.

**Architecture:** Migration `0050` adds locator, event, mirror-job, and copy-idempotency tables. Backend and doc worker use focused locator services around those tables; worker reconciliation writes the two-sheet WeCom mirror asynchronously, while `/sync/` presents the same dynamic asset catalog used by downloads. Existing provider and export implementations remain compatibility adapters.

**Tech Stack:** PostgreSQL 16, Python 3.12, FastAPI, psycopg 3, unittest, vanilla HTML/JavaScript, nginx, Docker Compose, GitHub Actions.

## Global Constraints

- AliECS is public: no real `dc...`, `s3_...`, admin userid, group ID, credential, token, or secret may enter Git, logs, tests, PR text, or API responses.
- `api_doc_id` and `share_ref` are separate; only a verified nonempty API ID can reach a provider or create a sync request.
- Production PostgreSQL is the machine source of truth and is covered by existing encrypted database backups; the WeCom workbook is a one-way human mirror.
- Credentials remain in infra SOPS; the locator stores only `env_profile` and a non-secret `credential_ref`.
- Names are display data, never keys. Provider/profile/API ID or unresolved share reference form identity.
- P4 doc/T+ scheduler modes remain exactly `shadow`; this plan never switches `active`.
- Use `python -m unittest discover -s tests -p "test_xxx.py"`; do not use package-style `tests.<module>` imports.
- Every database write explicitly commits or rolls back; mirror failure is fail-open for source sync but retained for retry and alerting.
- `/sync/` and `/v1/sync/*` never return external document IDs, share IDs, admin userids, or credential values.
- Git changes use the current feature branch and explicit file lists; never `git add -A`.

---

### Task 1: PostgreSQL locator and idempotency schema

**Files:**
- Create: `db/migrations/0050_document_locator_registry.sql`
- Create: `tests/test_migration_document_locator_registry.py`
- Create: `tests/test_document_locator_storage.py`
- Modify: `services/doc-sync-worker/app/storage/postgres.py`

**Interfaces:**
- Produces: `PostgresStore.upsert_document_locator(locator: dict[str, Any], *, event_type: str, actor: str) -> dict[str, Any]`
- Produces: `PostgresStore.enqueue_document_locator_mirror(locator_id: int, locator_version: int, trigger: str) -> int`
- Produces: `claim_document_locator_mirror_jobs(limit: int)`, `finish_document_locator_mirror_job(job_id: int)`, and `retry_document_locator_mirror_job(job_id: int, error: str, delay_seconds: int)`.
- Produces tables `document_locator_registry`, `document_locator_events`, `document_locator_mirror_jobs`, `document_copy_requests`.

- [ ] **Step 1: Write migration contract tests**

Assert the SQL defines nullable `api_doc_id`, separate `share_ref`, JSONB admin/capability fields, partial unique indexes for resolved and unresolved identities, FK links to `external_sources`, explicit timestamps, append-only events, retryable mirror jobs, and unique copy `idempotency_key`. Add a repository fake-cursor test proving all upserts write `updated_at=NOW()` and rollback on an injected event insert failure.

- [ ] **Step 2: Run RED**

Run: `python -m unittest discover -s tests -p "test_migration_document_locator_registry.py"` and `python -m unittest discover -s tests -p "test_document_locator_storage.py"`

Expected: failure because migration 0050 and locator repository methods do not exist.

- [ ] **Step 3: Add schema and repository primitives**

Use these stable columns and constraints:

```sql
CREATE TABLE document_locator_registry (
  id BIGSERIAL PRIMARY KEY,
  provider TEXT NOT NULL,
  env_profile TEXT NOT NULL,
  api_doc_id TEXT,
  share_ref TEXT,
  document_name TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  admin_userids JSONB NOT NULL DEFAULT '[]'::jsonb,
  credential_ref TEXT NOT NULL DEFAULT '',
  source_kind TEXT NOT NULL,
  lifecycle_status TEXT NOT NULL,
  syncability_status TEXT NOT NULL,
  capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
  sheet_count INTEGER NOT NULL DEFAULT 0,
  external_source_id BIGINT REFERENCES external_sources(id) ON DELETE SET NULL,
  locator_version INTEGER NOT NULL DEFAULT 1,
  registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_verified_at TIMESTAMPTZ,
  last_sync_at TIMESTAMPTZ,
  last_error_code TEXT NOT NULL DEFAULT '',
  last_error_summary TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (lifecycle_status IN ('active','disabled','unresolved')),
  CHECK (syncability_status IN ('verified','unverified','invalid-id','permission-denied')),
  CHECK (api_doc_id IS NOT NULL OR share_ref IS NOT NULL)
);
```

Repository upsert must lock an existing identity row, bump `locator_version` only when key fields change, insert a redacted event listing changed field names, and enqueue a version-deduplicated mirror job in the same transaction.

- [ ] **Step 4: Run GREEN and adjacent storage tests**

Run the two new test files, then `python -m unittest discover -s tests -p "test_sync_scheduler_storage.py"` and `python -m unittest discover -s tests -p "test_doc_sync_job_platform.py"`.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/0050_document_locator_registry.sql tests/test_migration_document_locator_registry.py tests/test_document_locator_storage.py services/doc-sync-worker/app/storage/postgres.py
git commit -m "feat(sync): add private document locator registry"
```

### Task 2: Private registry importer and source association

**Files:**
- Create: `services/doc-sync-worker/app/pipelines/document_locator_import.py`
- Create: `tests/test_document_locator_import.py`
- Modify: `services/doc-sync-worker/app/main.py`
- Modify: `services/doc-sync-worker/app/storage/postgres.py`

**Interfaces:**
- Consumes: Task 1 locator repository.
- Produces: `import_document_locators(payload: dict[str, Any], store: Any) -> dict[str, int]`.
- Produces CLI: `python -m app.main import-document-locators`, reading UTF-8 JSON only from stdin and printing only `{inserted, updated, linked, unresolved, conflicts}`.

- [ ] **Step 1: Write importer RED tests**

Cover: valid `dc` input matched to an existing doc anchor; live production name/profile winning over stale registry name; `s3_` stored only in `share_ref`; missing profile accepted only when an existing source uniquely supplies it; new unmatched resolved item requires explicit profile; duplicate input dedupes by ID; conflicting profile is counted and not mutated; stdout contains no input identifiers or admin userid.

- [ ] **Step 2: Run RED**

Run: `python -m unittest discover -s tests -p "test_document_locator_import.py"`

Expected: import module and CLI command are absent.

- [ ] **Step 3: Implement importer**

Normalize both legacy registry shapes:

```python
def registry_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "api_doc_id": docid if valid_wecom_docid(docid) else None,
            "share_ref": share_ref_from_url(item.get("url", "")) or (docid if docid.startswith("s3_") else None),
            "document_name": str(item.get("doc_name") or ""),
            "source_url": str(item.get("url") or ""),
            "admin_userids": [str(item["admin_userid"])] if item.get("admin_userid") else [],
            "source_kind": "registry",
        }
        for docid, item in (payload.get("docs") or {}).items()
    ]
```

Resolve profile/source association by exact API ID or exact share reference only. Never match by name. Wrap each entry in a savepoint so one conflict does not lose other valid imports, and keep output count-only.

- [ ] **Step 4: Run GREEN plus CLI syntax/import isolation**

Run importer tests, `python -m py_compile` on new/modified files, and existing doc worker CLI tests from `test_doc_sync_worker.py`.

- [ ] **Step 5: Commit**

```bash
git add services/doc-sync-worker/app/pipelines/document_locator_import.py services/doc-sync-worker/app/main.py services/doc-sync-worker/app/storage/postgres.py tests/test_document_locator_import.py
git commit -m "feat(sync): import private document locator registries"
```

### Task 3: Immediate locator reconciliation and concise WeCom mirror

**Files:**
- Create: `services/doc-sync-worker/app/pipelines/document_locator.py`
- Create: `services/doc-sync-worker/app/pipelines/document_locator_mirror.py`
- Create: `tests/test_document_locator_mirror.py`
- Modify: `services/doc-sync-worker/app/pipelines/sync_wecom_full.py`
- Modify: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`
- Modify: `services/doc-sync-worker/app/pipelines/worker_loop.py`
- Modify: `services/doc-sync-worker/app/pipelines/sync_alert_notifier.py`
- Modify: `tests/test_doc_sync_worker.py`
- Modify: `tests/test_sync_alert_notifier.py`

**Interfaces:**
- Consumes: Task 1 storage and Task 2 normalized identities.
- Produces: `reconcile_document_locators(store: Any, *, trigger: str) -> dict[str, int]`.
- Produces: `record_locator_after_request(store: Any, request: dict[str, Any], request_status: str) -> bool`.
- Produces: `run_pending_document_locator_mirror_jobs(limit: int = 10) -> int`.

- [ ] **Step 1: Write RED worker/mirror tests**

Assert every successful manual request, not only `copy-auto`, reconciles the locator and enqueues a mirror version; failed requests do not claim verified capabilities. Full WeCom/Feishu completion reconciles new/renamed/disabled docs. Mirror creates/uses exactly `文档定位档案` and `定位档案变更历史`, writes only agreed key fields, updates current rows by stable key, appends semantic events once, never writes old structure sheets, retries with redacted errors, and keeps source sync successful when the mirror provider fails.

- [ ] **Step 2: Run RED**

Run: `python -m unittest discover -s tests -p "test_document_locator_mirror.py"` and focused new cases in `test_doc_sync_worker.py`.

- [ ] **Step 3: Implement locator reconciliation**

Build locators from doc-level `external_sources`; infer `can_read=verified` only after successful provider sync, keep `can_write` unknown unless an actual controlled write has succeeded, and mark link-only rows `unresolved/invalid-id`. Call reconciliation after successful request and after full providers complete.

- [ ] **Step 4: Implement new two-sheet mirror**

Use the existing backup credential/profile and document ID env, but a new pipeline with these fixed field lists:

```python
CURRENT_FIELDS = (
    "平台", "企业配置", "文档名称", "文档定位ID", "来源链接", "管理员",
    "凭据引用", "来源类型", "生命周期状态", "可同步状态", "不可同步原因",
    "可读", "可写", "可创建副本", "工作表数量", "登记时间",
    "最后验证时间", "最后同步时间", "最后更新时间", "唯一键", "最近错误",
)
EVENT_FIELDS = (
    "事件时间", "文档名称", "事件类型", "触发来源", "变更字段", "状态摘要", "唯一键",
)
```

Do not invoke legacy daily structure snapshot generation from `worker_loop`; consume new mirror jobs every poll and after full reconciliation. Extend notifier conditions for pending-too-long or repeatedly failed mirror jobs.

- [ ] **Step 5: Run GREEN and regression suites**

Run mirror tests, full `test_wecom_structure_backup.py` to preserve rollback compatibility, `test_doc_sync_worker.py`, and `test_sync_alert_notifier.py`.

- [ ] **Step 6: Commit**

```bash
git add services/doc-sync-worker/app/pipelines/document_locator.py services/doc-sync-worker/app/pipelines/document_locator_mirror.py services/doc-sync-worker/app/pipelines/sync_wecom_full.py services/doc-sync-worker/app/pipelines/sync_feishu_full.py services/doc-sync-worker/app/pipelines/worker_loop.py services/doc-sync-worker/app/pipelines/sync_alert_notifier.py tests/test_document_locator_mirror.py tests/test_doc_sync_worker.py tests/test_sync_alert_notifier.py
git commit -m "feat(sync): mirror concise document locator records"
```

### Task 4: Unified backend catalog, download, copy, and docid repair

**Files:**
- Create: `services/backend-api/app/document_locator.py`
- Create: `tests/test_backend_document_locator.py`
- Modify: `services/backend-api/app/sync_control.py`
- Modify: `services/backend-api/app/routers/sync.py`
- Modify: `services/backend-api/app/routers/exports.py`
- Modify: `services/backend-api/app/integrations/wecom_docs.py`
- Modify: `tests/test_backend_sync_control.py`
- Modify: `tests/test_backend_sync_api.py`
- Modify: `tests/test_backend_exports.py`

**Interfaces:**
- Consumes: Task 1 tables.
- Produces canonical API `POST /v1/sync/assets/{source_id}/copy` with `{idempotency_key}`.
- Produces canonical API `PUT /v1/sync/assets/{source_id}/docid` with `{api_doc_id}` and no ID echo.
- Extends `GET /v1/sync/assets` items with `download_url`, `can_download`, `can_sync`, `can_copy`, `system_managed`, and redacted `reason`.
- Legacy `/v1/exports/*` actions delegate to the same service.

- [ ] **Step 1: Write RED service/API tests**

Cover dynamic catalog equality with exports, inclusion of `structure_backup_doc` as download-only/system-managed, link source as unresolved, no identifier leakage, canonical download/copy capabilities, copy idempotency across remote-created/local-retry states, single-transaction locator/source/request/mirror registration, docid repair exact read-only provider validation, profile ambiguity rejection, and HTTPException passthrough.

- [ ] **Step 2: Run RED**

Run the new backend locator test and the three focused backend suites.

- [ ] **Step 3: Extract unified asset catalog**

Move document aggregation out of the 900-line exports router into `document_locator.py`. Return only internal action URLs/IDs and booleans. Let exports catalog and sync assets call the same query so counts cannot drift.

- [ ] **Step 4: Implement idempotent copy and repair**

Persist a copy request before calling WeCom. After remote creation, store its API ID in the private copy row, then register locator/source/request/mirror atomically. A retry with the same key resumes registration and never calls `create_doc` twice. Repair accepts only exact `dc` shape, verifies `get_doc_name/get_sheets` with one unique profile, merges the unresolved locator and stale link source, and responds only with internal IDs/status.

- [ ] **Step 5: Run GREEN and backend regressions**

Run new/focused tests plus `python -m compileall -q services/backend-api/app`.

- [ ] **Step 6: Commit**

```bash
git add services/backend-api/app/document_locator.py services/backend-api/app/sync_control.py services/backend-api/app/routers/sync.py services/backend-api/app/routers/exports.py services/backend-api/app/integrations/wecom_docs.py tests/test_backend_document_locator.py tests/test_backend_sync_control.py tests/test_backend_sync_api.py tests/test_backend_exports.py
git commit -m "feat(sync): converge document asset actions"
```

### Task 5: `/sync/` UI migration and safe redirects

**Files:**
- Modify: `services/public-web/sync/index.html`
- Modify: `services/public-web/exports/index.html`
- Modify: `services/public-web/nginx.conf`
- Modify: `services/public-web/tplus-sync/index.html`
- Modify: `tests/test_sync_frontend.py`
- Modify: `tests/test_exports_frontend.py`
- Modify: `tests/test_tplus_sync_frontend.py`

**Interfaces:**
- Consumes: Task 4 capability fields and canonical APIs.
- Produces one functional asset UI at `/sync/?view=assets`; `/exports/` and `/tplus-sync/` are compatibility redirects.

- [ ] **Step 1: Write strict Node RED tests**

Assert download/copy/sync buttons follow capabilities; system asset is download-only; unresolved item shows reason; copy generates one idempotency key per user action and reuses it on retry; stale responses/logout cannot commit; no external ID is rendered; `/exports/` redirects to `/sync/?view=assets`; `/tplus-sync/` Location is relative and cannot downgrade to HTTP.

- [ ] **Step 2: Run RED**

Run the three frontend test files with `NODE_OPTIONS=--unhandled-rejections=strict`.

- [ ] **Step 3: Implement asset actions**

Add download via `AliECSAdmin.downloadExport`, canonical copy with `crypto.randomUUID()` fallback, and existing latest-session/control-busy guards. Keep dynamic values escaped and use numeric array indexes rather than embedding job/source strings in handlers.

- [ ] **Step 4: Implement redirects**

Use nginx relative redirects (`absolute_redirect off` in the exact compatibility locations or an equivalent literal relative `Location`) so external HTTPS never becomes `http://`. Keep tiny HTML meta fallbacks for image rollback only.

- [ ] **Step 5: Run GREEN and adjacent asset tests**

Run frontend suites, inline JavaScript syntax extraction under Node, common admin asset tests, and nginx/Compose config validation.

- [ ] **Step 6: Commit**

```bash
git add services/public-web/sync/index.html services/public-web/exports/index.html services/public-web/nginx.conf services/public-web/tplus-sync/index.html tests/test_sync_frontend.py tests/test_exports_frontend.py tests/test_tplus_sync_frontend.py
git commit -m "feat(sync): move export and copy actions into assets"
```

### Task 6: PostgreSQL integration, CI, navigation, and release evidence

**Files:**
- Create: `tests/test_document_locator_integration.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/project-ai-map.md`
- Modify: `docs/superpowers/specs/2026-08-14-unified-sync-registry-convergence-design.md`

**Interfaces:**
- Consumes all previous tasks.
- Produces opt-in `DOCUMENT_LOCATOR_INTEGRATION_DATABASE_URL` PostgreSQL 16 test and production evidence without identifiers.

- [ ] **Step 1: Write real PostgreSQL integration test**

Apply all migrations, import a synthetic resolved and unresolved registry, reconcile sources/jobs, exercise mirror savepoint failure, copy registration idempotency, exact cleanup, and assert the connection remains queryable. Use unique `ci.locator.<uuid>` values that satisfy validators without resembling real production identifiers.

- [ ] **Step 2: Wire CI and docs**

Run the opt-in locator test after migrations and existing P1-P5 integration tests but before backend smoke. Document new modules/routes/tables and retain no production counts or IDs in project navigation.

- [ ] **Step 3: Run complete local verification**

Run focused suites first, then root unittest, full T+ unittest, navigation validator, Python compile, strict Node, local/business Compose config, nginx config, `git diff --check`, and repository-wide identifier/secret scans. The opt-in test must be exactly one expected skip without a database URL.

- [ ] **Step 4: Commit**

```bash
git add tests/test_document_locator_integration.py .github/workflows/ci.yml docs/project-ai-map.md docs/superpowers/specs/2026-08-14-unified-sync-registry-convergence-design.md
git commit -m "test(sync): verify document locator convergence"
```

- [ ] **Step 5: PR, CI, merge, and deploy**

Push the feature branch, open a ready PR, require validate and migration-dry-run success, and inspect the real PostgreSQL integration log for `Ran 1 test` / `OK` without skip. Squash merge, wait for all GHCR builds, dispatch `release-deploy.yml -f deploy_target=business-cn`, require `stage-business-cn-peer=success`, and verify new backend/public/doc-worker images on txecs. `deploy-business-cn=skipped` remains expected.

- [ ] **Step 6: Import private registries and production acceptance**

Build one in-memory JSON payload from both local registry files and pipe it directly to `docker exec ... python -m app.main import-document-locators`; do not create a public file or echo the payload. Verify count-only result: 14 unique, 13 linked/verified-capable, 1 unresolved. Consume mirror jobs, then verify the two authoritative sheets and dynamic catalog counts without printing locator values.

- [ ] **Step 7: Controlled copy and final checks**

Create exactly one copy of a non-system WeCom test-safe document through canonical `/v1/sync/assets/{id}/copy`, wait for its initial request and mirror job to succeed, and prove locator/source/job/download/event convergence. Re-run eligible-source/job bidirectional difference queries, invalid/system request count, external-ID leak scans, public redirects, container scheduler modes (`shadow`), and open alerts. Record only internal request IDs, names, counts, commit/run/digest evidence, and any unresolved external prerequisite.
