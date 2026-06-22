# WeCom Structure Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在企微 A 创建结构备份智能表格，每日保存企微 A/B 全部文档的 docid、sheet_id、名称和规范化字段结构，并让 `/health/` 创建副本可靠入队和写入。

**Architecture:** doc-sync worker 从现有 Postgres `external_sources` / `external_fields` 生成稳定结构快照，通过持久化 `wecom_structure_backup_jobs` 队列写入企微 A 的三张备份工作表。每日全量同步和 backend `copy-auto` 分别创建幂等任务，writer 以文档唯一键和结构版本唯一键实现 upsert 与历史去重。

**Tech Stack:** Python 3.11、requests、psycopg 3、PostgreSQL、企业微信文档/智能表格 OpenAPI、pytest/unittest、Docker Compose。

**Execution note:** 用户要求连续执行且未授权提交或推送，因此本计划不执行 `git commit` / `git push`，只保留隔离 worktree diff。

---

## File Map

- Create `db/migrations/0017_wecom_structure_backup.sql`: 持久化备份任务队列。
- Create `services/doc-sync-worker/app/pipelines/wecom_structure_backup.py`: 快照规范化、哈希、企微 schema 初始化、最新结构/历史写入和任务消费。
- Modify `services/doc-sync-worker/app/providers/wecom.py`: 增加创建文档、工作表、字段及记录增改 API。
- Modify `services/doc-sync-worker/app/storage/postgres.py`: 读取文档结构并管理备份任务。
- Modify `services/doc-sync-worker/app/pipelines/sync_wecom_full.py`: 每日同步完成后入队，copy-auto 同步后保证任务可消费。
- Modify `services/doc-sync-worker/app/pipelines/worker_loop.py`: 每轮同步请求后消费备份任务。
- Modify `services/doc-sync-worker/app/main.py`: 增加初始化与人工同步 CLI。
- Modify `services/backend-api/app/main.py`: 创建副本事务中插入 copy-auto 备份任务。
- Modify `deploy/ecs/compose.prod.yml`, `deploy/ecs/deploy.sh`, `deploy/ecs/release-meta.env.example`: 传递结构备份配置。
- Modify `services/doc-sync-worker/README.md`, `CHANGELOG.md`, `VERSION`: 运维说明和版本。
- Create `tests/test_wecom_structure_backup.py`: 结构快照、writer、任务消费的 TDD 测试。
- Modify `tests/test_backend_exports.py`, `tests/test_doc_sync_worker.py`: copy-auto 入队与 worker loop 回归测试。

### Task 1: Add durable backup job storage

**Files:**
- Create: `db/migrations/0017_wecom_structure_backup.sql`
- Modify: `services/doc-sync-worker/app/storage/postgres.py`
- Test: `tests/test_wecom_structure_backup.py`

- [ ] **Step 1: Write failing storage tests**

Add tests using a fake cursor/connection to assert these public methods and SQL contracts:

```python
store.enqueue_structure_backup_job(source_id=42, trigger="daily", event_key="daily:2026-06-21:COMPANY_A:doc-1")
jobs = store.pending_structure_backup_jobs(limit=10)
store.mark_structure_backup_job_running(7)
store.retry_structure_backup_job(7, "temporary failure", delay_seconds=60)
store.finish_structure_backup_job(7)
```

Assert `ON CONFLICT(event_key) DO NOTHING`, `status='pending'`, `next_attempt_at <= NOW()`, retry attempt increment, and success timestamps.

- [ ] **Step 2: Run RED test**

Run:

```powershell
python -m pytest -q tests/test_wecom_structure_backup.py
```

Expected: import or attribute failure because the job methods and migration do not exist.

- [ ] **Step 3: Add migration**

Create an idempotent table:

```sql
CREATE TABLE IF NOT EXISTS wecom_structure_backup_jobs (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
    event_key TEXT NOT NULL UNIQUE,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_wecom_structure_backup_jobs_pending
    ON wecom_structure_backup_jobs(status, next_attempt_at, id);
```

- [ ] **Step 4: Implement store methods**

Add `enqueue_structure_backup_job`, `pending_structure_backup_jobs`, `mark_structure_backup_job_running`, `retry_structure_backup_job`, and `finish_structure_backup_job`. Retry SQL must set `status='pending'`, increment attempts, store a sanitized error, and calculate `next_attempt_at` from a passed delay.

- [ ] **Step 5: Run GREEN test**

Run `python -m pytest -q tests/test_wecom_structure_backup.py`.
Expected: storage tests pass.

### Task 2: Build canonical document snapshots

**Files:**
- Create: `services/doc-sync-worker/app/pipelines/wecom_structure_backup.py`
- Modify: `services/doc-sync-worker/app/storage/postgres.py`
- Test: `tests/test_wecom_structure_backup.py`

- [ ] **Step 1: Write failing snapshot tests**

Test a document with two sheets and mixed field response shapes. The wished-for API is:

```python
snapshot = build_document_snapshot(source_row, sheet_rows, max_sheets=20)
assert snapshot.unique_key == "COMPANY_A:dc-1"
assert snapshot.structure_hash == build_document_snapshot(source_row, list(reversed(sheet_rows)), 20).structure_hash
assert snapshot.sheet_slots[0]["工作表01编码"] == "sheet-a"
assert json.loads(snapshot.sheet_slots[0]["工作表01字段结构"])[0] == {
    "id": "field-1",
    "name": "名称",
    "type": "FIELD_TYPE_TEXT",
    "order": 1,
    "config": {"is_primary": True},
}
```

Also assert record counts and raw business data are absent, volatile keys do not affect the hash, formula/select/link config does affect the hash, and 21 sheets raise `StructureBackupError`.

- [ ] **Step 2: Run RED test**

Run `python -m pytest -q tests/test_wecom_structure_backup.py`.
Expected: module or symbol import failure.

- [ ] **Step 3: Implement document structure query**

Add `PostgresDocSyncStore.list_wecom_document_structures(source_id: int | None = None)`. It must return active doc rows with ordered active sheet rows and each sheet's ordered `external_fields.raw_json`; do not load `external_records.normalized_json`.

- [ ] **Step 4: Implement normalization and hashing**

Create:

```python
@dataclass(frozen=True)
class DocumentStructureSnapshot:
    source_id: int
    env_profile: str
    docid: str
    document_name: str
    unique_key: str
    structure_hash: str
    values: dict[str, Any]

class StructureBackupError(RuntimeError):
    pass
```

Normalize field ID/name/type/order plus `is_primary` and `property_*` keys that represent select, formula, reference/link, date/number/currency/percentage and auto-number behavior. Serialize with `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`. Populate fixed `工作表01...工作表20` column groups and compute SHA-256 over document name plus normalized sheet/field structure.

- [ ] **Step 5: Run GREEN test**

Run `python -m pytest -q tests/test_wecom_structure_backup.py`.
Expected: snapshot tests pass.

### Task 3: Add WeCom write API and schema bootstrap

**Files:**
- Modify: `services/doc-sync-worker/app/providers/wecom.py`
- Modify: `services/doc-sync-worker/app/pipelines/wecom_structure_backup.py`
- Modify: `services/doc-sync-worker/app/main.py`
- Test: `tests/test_wecom_structure_backup.py`

- [ ] **Step 1: Write failing provider/bootstrap tests**

Test that `WeComSmartsheetClient` sends exact paths and payloads for:

```python
client.create_doc("企微智能表格结构备份", ["WangHao"])
client.add_sheet(docid, "企微A-最新结构", 1)
client.add_fields(docid, sheet_id, fields)
client.add_records(docid, sheet_id, records)
client.update_records(docid, sheet_id, records)
```

Test `ensure_backup_schema(client, configured_docid="")` creates the doc, resolves three sheet IDs, creates missing fields, and returns a result containing `docid`, `url`, and sheet mapping. With a configured docid it must be idempotent and not create another document.

- [ ] **Step 2: Run RED test**

Run `python -m pytest -q tests/test_wecom_structure_backup.py`.
Expected: provider methods and bootstrap function are missing.

- [ ] **Step 3: Implement provider methods**

Use existing `_post` and return normalized results. `create_doc` must require a non-empty `docid`; record methods use `key_type='CELL_VALUE_KEY_TYPE_FIELD_TITLE'` and batches of at most 50 records.

- [ ] **Step 4: Implement schema bootstrap and CLI**

Define exact sheet names and fields in `wecom_structure_backup.py`. Add `init-wecom-structure-backup` to `app.main`; it reads `WECOM_STRUCTURE_BACKUP_PROFILE` (default `COMPANY_A`), credentials, `WECOM_DOC_ADMIN_USERS`, and optional `WECOM_STRUCTURE_BACKUP_DOCID`. Output only docid, sanitized URL, and sheet IDs; never output secrets.

- [ ] **Step 5: Run GREEN test**

Run `python -m pytest -q tests/test_wecom_structure_backup.py tests/test_doc_sync_worker.py`.
Expected: provider/bootstrap and existing CLI tests pass.

### Task 4: Upsert latest rows and version history

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/wecom_structure_backup.py`
- Test: `tests/test_wecom_structure_backup.py`

- [ ] **Step 1: Write failing writer tests**

Use an in-memory fake client with existing backup records. Verify:

```python
result = backup_snapshot(client, sheet_ids, snapshot, trigger="daily")
assert result.action == "created"
assert fake_client.added_latest == 1
assert fake_client.added_history == 1
```

Then run same hash and assert latest is updated but history is unchanged. Run a changed field name and assert latest is updated plus one new history version with `版本唯一键=COMPANY_A:dc-1:<new_hash>` and `前一结构哈希=<old_hash>`. Test duplicate retry does not duplicate history.

- [ ] **Step 2: Run RED test**

Run `python -m pytest -q tests/test_wecom_structure_backup.py`.
Expected: writer symbols missing.

- [ ] **Step 3: Implement writer**

Read target latest/history records with existing paginated `get_records`, map latest by `唯一键` and history by `版本唯一键`, and call add/update record APIs. Build a concise change summary from document rename, sheet add/remove/rename, and field add/remove/rename/type/config changes.

- [ ] **Step 4: Run GREEN test**

Run `python -m pytest -q tests/test_wecom_structure_backup.py`.
Expected: writer tests pass.

### Task 5: Wire daily and copy-auto jobs

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/wecom_structure_backup.py`
- Modify: `services/doc-sync-worker/app/pipelines/sync_wecom_full.py`
- Modify: `services/doc-sync-worker/app/pipelines/worker_loop.py`
- Modify: `services/backend-api/app/main.py`
- Modify: `services/doc-sync-worker/app/main.py`
- Test: `tests/test_wecom_structure_backup.py`
- Test: `tests/test_backend_exports.py`
- Test: `tests/test_doc_sync_worker.py`

- [ ] **Step 1: Write failing integration tests**

Add a backend copy test whose fake cursor asserts both `_create_doc_sync_request(..., requested_by='copy-auto')` and `_enqueue_structure_backup_job(..., trigger='copy-auto')` occur in the same transaction.

Add worker tests asserting full sync enqueues `daily:<UTC date>:<profile>:<docid>` jobs after all profile syncs and poll cycles call sync request consumption before structure backup job consumption.

Add consumer tests asserting success finishes a job, missing source structure retries it, and API failure retries with a sanitized error.

- [ ] **Step 2: Run RED tests**

Run:

```powershell
python -m pytest -q tests/test_wecom_structure_backup.py tests/test_backend_exports.py tests/test_doc_sync_worker.py
```

Expected: enqueue helpers and worker callbacks are absent.

- [ ] **Step 3: Implement backend enqueue**

Add `_enqueue_structure_backup_job(cur, source_id, trigger, event_key)` with `ON CONFLICT(event_key) DO NOTHING`. In the copy route use `event_key=f"copy-auto:{new_docid}"` immediately after doc registration and sync request creation.

- [ ] **Step 4: Implement worker enqueue and consume**

After `run_sync_wecom_full` finishes profiles, enqueue one daily job per active doc source. Add `run_pending_structure_backup_jobs(limit=10)` and call it after `run_pending_sync_requests` in each poll. Add CLI `sync-wecom-structure-backup` to enqueue all docs and consume until no pending ready jobs remain.

- [ ] **Step 5: Run GREEN tests**

Run the three-file pytest command from Step 2.
Expected: all pass.

### Task 6: Add deployment configuration and release metadata

**Files:**
- Modify: `deploy/ecs/compose.prod.yml`
- Modify: `deploy/ecs/deploy.sh`
- Modify: `deploy/ecs/release-meta.env.example`
- Modify: `services/doc-sync-worker/README.md`
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Test: existing config/version checks

- [ ] **Step 1: Write failing static assertions**

Extend `tests/test_doc_sync_worker.py` to assert compose passes:

```text
WECOM_STRUCTURE_BACKUP_ENABLED
WECOM_STRUCTURE_BACKUP_DOCID
WECOM_STRUCTURE_BACKUP_PROFILE
WECOM_STRUCTURE_BACKUP_MAX_SHEETS
WECOM_DOC_ADMIN_USERS
```

Assert `release-meta.env.example` documents safe defaults and deploy script preserves the values.

- [ ] **Step 2: Run RED test**

Run `python -m pytest -q tests/test_doc_sync_worker.py`.
Expected: missing configuration assertions fail.

- [ ] **Step 3: Add configuration and docs**

Default `WECOM_STRUCTURE_BACKUP_ENABLED=false`, profile `COMPANY_A`, max sheets `20`, and empty docid. Update README with bootstrap, enable, manual sync, retry inspection, and no-secret logging commands.

- [ ] **Step 4: Bump version**

Set `VERSION` to `v2.1.13` and add the first changelog section describing structure backup, daily/copy-auto jobs, and live bootstrap.

- [ ] **Step 5: Run GREEN checks**

Run:

```powershell
python -m pytest -q tests/test_doc_sync_worker.py
python scripts/validate_version.py
```

Expected: pass and `VERSION 校验通过：v2.1.13`.

### Task 7: Full local verification

**Files:** all changed files

- [ ] **Step 1: Run focused tests**

```powershell
python -m pytest -q tests/test_wecom_structure_backup.py tests/test_wecom_sheet_sync.py tests/test_doc_sync_worker.py tests/test_backend_exports.py tests/test_backend_wecom_docs.py
```

Expected: zero failures.

- [ ] **Step 2: Run full suite**

```powershell
python -m pytest -q
python scripts/validate_version.py
```

Expected: zero failures and valid version.

- [ ] **Step 3: Review diff and secret hygiene**

```powershell
git diff --check
git status --short
git diff --stat
rg -n "corpsecret|access_token|WECOM_COMPANY_A_APP_SECRET=" . -g '!*.example' -g '!tests/**'
```

Expected: no whitespace errors, only planned files, and no credential values.

### Task 8: Create backup workbook, deploy, and verify live

**Files:** ECS runtime env only for the generated docid and enable flag; no credential changes

- [ ] **Step 1: Hot-update worker/backend for online validation**

Build/tag images using the existing ECS deployment flow, hot-update the backend/doc-sync worker, and apply migration `0017_wecom_structure_backup.sql`. Verify compose renders before restart.

- [ ] **Step 2: Bootstrap the WeCom A workbook**

Run in the deployed worker:

```bash
python -m app.main init-wecom-structure-backup
```

Capture the returned `dc*` docid and sanitized URL. Store only the docid in ECS runtime env as `WECOM_STRUCTURE_BACKUP_DOCID`; set `WECOM_STRUCTURE_BACKUP_ENABLED=true`; restart worker.

- [ ] **Step 3: Run initial backup**

```bash
python -m app.main sync-wecom-structure-backup
```

Expected: 企微 A/B latest sheets contain one row per active non-backup document, and history contains one initial version per document.

- [ ] **Step 4: Verify idempotence and copy-auto path**

Run the manual sync command again and confirm history count does not increase. Enqueue a compensation `copy-auto` job for an existing copy source and confirm it is consumed without duplicating its version history.

- [ ] **Step 5: Verify runtime health**

Check `docker compose ps`, backend `/api/healthz`, `deploy/ecs/healthcheck.sh`, `deploy/ecs/post-deploy-smoke.sh`, job status counts, worker error logs, and live `/health/` availability. Report any manual-only or secret-dependent remainder explicitly.
