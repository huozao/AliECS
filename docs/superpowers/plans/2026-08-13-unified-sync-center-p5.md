# 统一同步中心 P5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把全部同步配置、触发、资产与表级作业收敛到 `/sync/`，将 `/exports/` 精简为四类纯下载页，并把旧 T+ 页面重定向到统一中心。

**Architecture:** backend 新增无外部 API 调用的 `sync_control.py` 作为配置与请求队列服务，`sync.py` 暴露 canonical 写 API，旧 ops/exports 写端点只保留兼容委托。doc worker 用真实 `external_sources` allowlist 对账 `sync_jobs`，只建目录不建 run；前端把控制面并入现有 generation-safe `/sync/`，`/exports/` 只读 catalog/download。

**Tech Stack:** Python 3.12、FastAPI、psycopg 3 / PostgreSQL 16、原生 HTML/CSS/JavaScript、Python unittest、Node VM 行为探针、nginx 1.27。

## Global Constraints

- AliECS 是 PUBLIC 仓库：真实企微 docid、`s3_` 分享 ID、飞书 token、群 ID、凭据、生产 env 一律不入仓库、测试 fixture 或日志。
- backend-api 不直接调用企微/飞书/T+ provider；所有手动同步只写现有 request 表。
- 只允许企微 `smartsheet_sheet`、飞书 `bitable_table` 建表级 job；link、structure、disabled 来源必须排除。
- `s3_` 不能转换为 `dc`，只能标记“缺少有效企微 docid”，不得入队。
- 目录对账不得创建 `sync_job_runs`；历史 job 只禁用不删除。
- `sync_jobs.updated_at` 没有 trigger，所有 upsert/update 必须显式 `updated_at=NOW()`。
- P4 doc/T+ 继续 `shadow`，P5 不切 `active`，不改变 legacy 调度控制流。
- `/exports/` 只保留分类、目录、下载；同步、设置、创建副本全部移出页面。
- Git 走 `codex/unified-sync-center-p5` 分支 + PR；只 add 明确文件，禁止 `git add -A`。
- 测试使用 `python -m unittest discover -s tests -p "test_xxx.py"`，不使用 `python -m unittest tests.<module>`。

---

### Task 1: Canonical sync control service and API

**Files:**
- Create: `services/backend-api/app/sync_control.py`
- Modify: `services/backend-api/app/routers/sync.py`
- Modify: `services/backend-api/app/routers/ops.py`
- Modify: `services/backend-api/app/routers/exports.py`
- Modify: `services/backend-api/app/sync_read.py`
- Test: `tests/test_backend_sync_control.py`
- Modify test: `tests/test_backend_sync_api.py`
- Modify test: `tests/test_backend_exports.py`
- Modify test: `tests/test_backend_ops_status.py`

**Interfaces:**
- Produces: `SyncConfigUpdate`, `DocSyncConfigUpdate` Pydantic models.
- Produces: `assets(conn, tplus_items) -> dict[str, Any]` with fixed four groups and no external IDs.
- Produces: `read_tplus_config(connect)`, `save_tplus_config(connect, body, user_sub)`, `read_doc_config(connect)`, `save_doc_config(connect, body, user_sub)`.
- Produces: `enqueue_tplus_full(conn, user_sub)`, `enqueue_doc_asset(conn, source_id, requested_by)`, `enqueue_doc_job(conn, job_key, requested_by)`, `enqueue_all(conn, requested_by)`.
- Consumers: `/v1/sync/*` canonical routes and legacy aliases in `ops.py`/`exports.py`.

- [ ] **Step 1: Write failing service tests**

Add table-driven tests whose literal fixtures prove:

```python
def test_assets_use_four_groups_without_external_ids(self):
    result = sync_control.assets(conn, tplus_items=[{"name": "bom", "download_url": "/x"}])
    self.assertEqual(["tplus", "wecom_company_a", "wecom_company_b", "feishu"],
                     [group["key"] for group in result["groups"]])
    self.assertNotIn("external_doc_id", json.dumps(result))

def test_wecom_link_is_visible_but_not_syncable(self):
    item = sync_control.assets(conn, tplus_items=[])["groups"][1]["items"][0]
    self.assertEqual(False, item["syncable"])
    self.assertEqual("缺少有效企微 docid", item["reason"])

def test_enqueue_all_excludes_link_structure_and_duplicate_requests(self):
    result = sync_control.enqueue_all(conn, "admin")
    self.assertEqual({"documents_queued": 2, "documents_skipped": 1,
                      "tplus_queued": True}, selected_fields(result))
```

Use a recording cursor for exact SQL/parameters and a failing cursor for rollback. Include valid/invalid doc anchor source types, disabled rows, a `s3_`-shaped value, pending/running duplicate, and T+ duplicate.

- [ ] **Step 2: Run RED**

Run:

```powershell
python -m unittest discover -s tests -p "test_backend_sync_control.py" -v
```

Expected: import or attribute failures because `app.sync_control` and its interfaces do not exist.

- [ ] **Step 3: Implement minimal `sync_control.py`**

Implement the fixed source predicates and response boundary:

```python
WECOM_DOC_TYPES = {"smartsheet_doc", "registry_doc"}
WECOM_TABLE_TYPE = "smartsheet_sheet"
FEISHU_DOC_TYPE = "bitable_app"
FEISHU_TABLE_TYPE = "bitable_table"

def valid_wecom_docid(value: str) -> bool:
    return value.startswith("dc") and len(value) >= 80

def source_group(provider: str, env_profile: str) -> str:
    if provider == "chanjet": return "tplus"
    if provider == "feishu": return "feishu"
    return f"wecom_{env_profile.lower()}"
```

All queries select external IDs only for internal validation; response builders omit them. Document queue SQL must use `NOT EXISTS` against pending/running `sync_requests`. T+ queue SQL must preserve current `integration_sync_requests` dedupe semantics. Config saves update legacy config plus `sync_jobs.schedule` in one transaction and rollback on either failure.

- [ ] **Step 4: Add canonical router tests and routes**

Add tests for:

```text
GET  /v1/sync/assets
GET  /v1/sync/config/doc
PUT  /v1/sync/config/doc
GET  /v1/sync/config/tplus
PUT  /v1/sync/config/tplus
POST /v1/sync/run-all
POST /v1/sync/assets/{source_id}/run
POST /v1/sync/jobs/{job_key}/run
```

Each test calls the real FastAPI handler boundary with a fake connection, proves `require_admin`, `HTTPException` identity, sanitized 500, and 400/404 for unsupported jobs/sources. Implement routes in `routers/sync.py` using `closing(_conn())`; commit/rollback remains service-owned per operation.

- [ ] **Step 5: Make old endpoints compatibility aliases**

Move no SQL into router wrappers. `ops.py` and `exports.py` call the same service functions, preserving legacy response keys. Existing create-copy backend endpoint remains unchanged but no new code calls it.

- [ ] **Step 6: Enrich overview read model**

LEFT JOIN `external_sources` by `j.source_id` and return only:

```python
{
  "source_group": source_group(provider, env_profile),
  "env_profile": env_profile,
  "document_name": document_name,
  "sheet_name": sheet_name,
}
```

T+ jobs receive `source_group='tplus'`; no external ID column may enter `_OVERVIEW_SQL` or JSON.

- [ ] **Step 7: Run GREEN and regression suites**

Run:

```powershell
python -m unittest discover -s tests -p "test_backend_sync_control.py" -v
python -m unittest discover -s tests -p "test_backend_sync_api.py" -v
python -m unittest discover -s tests -p "test_backend_exports.py" -v
python -m unittest discover -s tests -p "test_backend_ops_status.py" -v
```

Expected: all pass; mutation check: changing the source allowlist, returning an ID, or removing rollback fails at least one test.

- [ ] **Step 8: Commit Task 1**

```powershell
git add services/backend-api/app/sync_control.py services/backend-api/app/routers/sync.py services/backend-api/app/routers/ops.py services/backend-api/app/routers/exports.py services/backend-api/app/sync_read.py tests/test_backend_sync_control.py tests/test_backend_sync_api.py tests/test_backend_exports.py tests/test_backend_ops_status.py
git commit -m "feat(sync): add unified control API"
```

---

### Task 2: Pre-catalog every active document table

**Files:**
- Modify: `services/doc-sync-worker/app/storage/sync_job_platform.py`
- Modify: `services/doc-sync-worker/app/pipelines/sync_wecom_full.py`
- Modify: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`
- Modify: `services/doc-sync-worker/app/pipelines/worker_loop.py`
- Modify test: `tests/test_doc_sync_job_platform.py`
- Modify test: `tests/test_doc_sync_worker.py`

**Interfaces:**
- Produces: `SyncJobPlatformWriter.reconcile_document_jobs() -> dict[str, int] | None`.
- Produces: `_reconcile_platform_jobs_fail_open(store) -> None` in the pipeline layer.
- Consumes: current `integration_sync_config(provider='doc_sync')`, existing P1 job key format and P4 schedule.

- [ ] **Step 1: Write failing writer tests**

Cover these observable mutations:

```python
def test_reconcile_upserts_only_active_table_sources_without_runs(self):
    result = writer.reconcile_document_jobs()
    self.assertEqual({"enabled": 2, "disabled": 1}, result)
    self.assertNotIn("INSERT INTO sync_job_runs", conn.sql)
    self.assertNotIn("smartsheet_link", conn.parameters)

def test_reconcile_preserves_nonempty_schedule_and_updates_timestamp(self):
    writer.reconcile_document_jobs()
    self.assertIn("schedule = CASE", conn.sql)
    self.assertIn("updated_at = NOW()", conn.sql)

def test_reconcile_rolls_back_and_is_fail_open(self):
    self.assertIsNone(writer.reconcile_document_jobs())
    self.assertEqual(1, conn.rollback_count)
```

- [ ] **Step 2: Run RED**

```powershell
python -m unittest discover -s tests -p "test_doc_sync_job_platform.py" -v
```

Expected: missing `reconcile_document_jobs` failures.

- [ ] **Step 3: Implement one-transaction reconciliation**

Use one CTE/upsert over eligible sources and one disable update. The upsert shape must be:

```sql
INSERT INTO sync_jobs(job_key, kind, provider, display_name, source_id, enabled, schedule, updated_at)
SELECT provider || '.doc.' || id, 'pull', provider, display_name, id, TRUE, legacy_schedule, NOW()
FROM eligible
ON CONFLICT(job_key) DO UPDATE SET
  kind=EXCLUDED.kind,
  provider=EXCLUDED.provider,
  display_name=EXCLUDED.display_name,
  source_id=EXCLUDED.source_id,
  enabled=TRUE,
  schedule=CASE WHEN sync_jobs.schedule='{}'::jsonb THEN EXCLUDED.schedule ELSE sync_jobs.schedule END,
  updated_at=NOW();
```

The disable query must be scoped to `kind='pull'`, providers wecom/feishu and non-null `source_id`, then use `NOT EXISTS` with the same eligibility predicate. Use `_best_effort` so failure rolls back and never escapes.

- [ ] **Step 4: Write failing pipeline lifecycle tests**

Prove:

- default production loop reconciles once before first scheduling decision;
- injected `full_sync`/`consume_requests` loop never opens DB for catalog work;
- successful and failed full sync/manual request both attempt final reconciliation after legacy bookkeeping;
- reconciliation failure never changes legacy exit/status and store closes exactly once.

- [ ] **Step 5: Run pipeline RED**

```powershell
python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v
```

Expected: only new catalog lifecycle tests fail.

- [ ] **Step 6: Wire fail-open lifecycle**

At default `run-loop` startup open a store, call `store.sync_jobs.reconcile_document_jobs()`, close in `finally`. In `run_sync_wecom_full`, `run_sync_feishu_full`, and `run_pending_sync_requests`, call the same writer after legacy `finish_run` / request finish and before closing the already-owned store. Do not invoke it in injected loop paths.

- [ ] **Step 7: Run GREEN and adjacent suites**

```powershell
python -m unittest discover -s tests -p "test_doc_sync_job_platform.py" -v
python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v
python -m unittest discover -s tests -p "test_sync_alert_notifier.py" -v
```

Expected: all pass and P4 scheduler call sequences remain byte-for-byte equivalent in legacy/shadow behavior tests.

- [ ] **Step 8: Commit Task 2**

```powershell
git add services/doc-sync-worker/app/storage/sync_job_platform.py services/doc-sync-worker/app/pipelines/sync_wecom_full.py services/doc-sync-worker/app/pipelines/sync_feishu_full.py services/doc-sync-worker/app/pipelines/worker_loop.py tests/test_doc_sync_job_platform.py tests/test_doc_sync_worker.py
git commit -m "feat(sync): reconcile document job catalog"
```

---

### Task 3: PostgreSQL 16 integration gate

**Files:**
- Create: `tests/test_sync_control_integration.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/project-ai-map.md`

**Interfaces:**
- Consumes: Task 1 `sync_control` and Task 2 real writer.
- Produces: opt-in env `SYNC_CONTROL_INTEGRATION_DATABASE_URL` and one real PostgreSQL test.

- [ ] **Step 1: Write the opt-in integration test**

The test must default to exactly one skip when env is absent. With PostgreSQL it must:

1. insert unique `ci.p5.*` fixtures containing valid table sources, a disabled source, a link source and a structure source; use only fake external IDs such as repeated `d` characters, never production values;
2. call the real doc writer reconciliation;
3. assert eligible source IDs equal enabled doc job source IDs, with no synthetic `sync_job_runs`;
4. call real `assets`, single job run and `enqueue_all`;
5. assert link/structure sources have zero requests and duplicate pending requests are skipped;
6. trigger a real DB error inside a savepoint, rollback it, then `SELECT 1` on the same connection;
7. in `finally`, delete by exact fixture IDs/keys and assert job/run/request/source residue is zero.

- [ ] **Step 2: Run default RED/GREEN boundary**

Before CI wiring, run:

```powershell
python -m unittest discover -s tests -p "test_sync_control_integration.py" -v
```

Expected after the test file is valid: `Ran 1 test`, `OK (skipped=1)`.

- [ ] **Step 3: Wire CI after migrations and prior sync integrations**

Add the command before backend smoke:

```yaml
- name: Sync control PostgreSQL integration
  env:
    SYNC_CONTROL_INTEGRATION_DATABASE_URL: postgresql://app:app@127.0.0.1:5432/app
  run: python -m unittest discover -s tests -p "test_sync_control_integration.py" -v
```

The hard merge gate is log evidence `... ok`, `Ran 1 test`, `OK` with no skip.

- [ ] **Step 4: Update navigation**

In `docs/project-ai-map.md`, replace P2-only wording with the canonical P5 read/write routes, point to `app/sync_control.py`, and document worker catalog reconciliation plus its integration command. Do not copy live counts or IDs.

- [ ] **Step 5: Verify and commit Task 3**

```powershell
python -m unittest discover -s tests -p "test_sync_control_integration.py" -v
python scripts/check_navigation.py
git add tests/test_sync_control_integration.py .github/workflows/ci.yml docs/project-ai-map.md
git commit -m "test(sync): verify P5 control plane on PostgreSQL"
```

---

### Task 4: `/sync/` becomes the sole control UI

**Files:**
- Modify: `services/public-web/sync/index.html`
- Modify test: `tests/test_sync_frontend.py`

**Interfaces:**
- Consumes: Task 1 canonical endpoints and enriched overview.
- Produces: `?group=tplus|wecom_company_a|wecom_company_b|feishu` preselection, four composable job filters, config forms and run actions.

- [ ] **Step 1: Write failing DOM and strict Node behavior tests**

Add static structure assertions for the exact controls and Node VM scenarios for:

- category + status + freshness + job/search compose over the same overview rows;
- `?group=tplus` preselects T+ without changing timeline offset incorrectly;
- assets render four groups, unsyncable item has reason and no handler, malicious names are escaped;
- doc/T+ config GET/PUT use canonical endpoints and preserve committed form state on reject;
- run-all, asset run and job run disable only the active button, then reload latest data;
- stale asset/config response cannot overwrite newer response;
- logout increments every generation, clears assets/config/jobs and late response cannot re-render;
- all promises pass under `NODE_OPTIONS=--unhandled-rejections=strict`.

- [ ] **Step 2: Run RED**

```powershell
python -m unittest discover -s tests -p "test_sync_frontend.py" -v
```

Expected: new control/filter behavior tests fail against the P2 read-only page.

- [ ] **Step 3: Add control and asset markup**

Add one “同步控制” section with all-sync, two config cards and `assetTabs`/`assetList`. Add four filters above `jobList`; do not add inline CSS outside existing shared styles except component-local layout already used by the page.

- [ ] **Step 4: Implement generation-safe state**

Extend state with committed values only:

```javascript
const state={
  overview:null, assets:null, docConfig:null, tplusConfig:null,
  group:initialGroup, jobStatus:'', freshness:'', jobQuery:'',
  controlRequestId:0, ...existingTimelineState
};
```

Every loader receives `{session,load}` and a per-resource request id. Only latest success commits state. Reject/finally restores buttons from committed state. `clearAdminData()` clears all new DOM/state and invalidates request IDs.

- [ ] **Step 5: Implement filters and actions**

Render overview rows from a pure `filteredJobs()` predicate. `runAll()`, `runAsset(sourceId)`, `runJob(jobKey)`, `saveDocConfig()` and `saveTplusConfig()` call only `/v1/sync/*`; all path values use `encodeURIComponent`, all text uses `esc`.

- [ ] **Step 6: Run GREEN and neighboring frontend suites**

```powershell
$env:NODE_OPTIONS='--unhandled-rejections=strict'
python -m unittest discover -s tests -p "test_sync_frontend.py" -v
python -m unittest discover -s tests -p "test_common_admin_assets.py" -v
python -m unittest discover -s tests -p "test_toast_frontend.py" -v
Remove-Item Env:NODE_OPTIONS
```

Also extract the inline script and run `node --check` on a temporary `.js` file without modifying the repo.

- [ ] **Step 7: Commit Task 4**

```powershell
git add services/public-web/sync/index.html tests/test_sync_frontend.py
git commit -m "feat(sync): add unified sync controls and filters"
```

---

### Task 5: Export-only page and T+ redirect

**Files:**
- Modify: `services/public-web/exports/index.html`
- Modify: `services/public-web/nginx.conf`
- Modify: `services/public-web/health/index.html`
- Modify test: `tests/test_exports_frontend.py`
- Modify test: `tests/test_tplus_sync_frontend.py`
- Modify test: `tests/test_health_frontend.py`
- Modify: `docs/project-navigation.md`

**Interfaces:**
- Produces: export page with GET/download only.
- Produces: exact nginx 301 `/tplus-sync/ -> /sync/?group=tplus`.

- [ ] **Step 1: Write failing export behavior tests**

The real inline script behavior probe must assert:

```text
GET /v1/exports/catalog is called
four tab groups render
downloadExport remains callable
zero POST/PUT/DELETE requests are emitted
no sync/copy/config controls or handlers exist
logout prevents late catalog response from rendering
```

- [ ] **Step 2: Write failing redirect/navigation tests**

Add a test that starts the built public-web nginx when Docker is available and asserts status 301 plus exact `Location: /sync/?group=tplus`; keep a pure config fallback test only for environments without Docker. Health navigation must link T+ directly to `/sync/?group=tplus` and contain one canonical sync-center card.

- [ ] **Step 3: Run RED**

```powershell
python -m unittest discover -s tests -p "test_exports_frontend.py" -v
python -m unittest discover -s tests -p "test_tplus_sync_frontend.py" -v
python -m unittest discover -s tests -p "test_health_frontend.py" -v
```

Expected: old sync/config/copy UI and missing redirect violate new tests.

- [ ] **Step 4: Slim `/exports/`**

Delete config section, sync-all button, per-item sync/copy actions, modal, corresponding functions and delayed refresh timers. Keep shared auth, catalog loading, tabs, file size, download and generation-safe logout. Add `<a href="/sync/">前往统一同步中心</a>`.

- [ ] **Step 5: Add exact redirect and update health navigation**

Place this before the generic HTML regex location:

```nginx
location = /tplus-sync/ {
  return 301 /sync/?group=tplus;
}
```

Update the health T+ link to the same URL. Update `docs/project-navigation.md` so `/sync/` is the control entry and `/exports/` is download-only.

- [ ] **Step 6: Run GREEN and static/runtime checks**

Run the three focused tests, inline `node --check`, and if Docker is available:

```powershell
docker build -t aliecs-public-web-p5-test services/public-web
docker run --rm -d --name aliecs-public-web-p5-test -p 18088:80 aliecs-public-web-p5-test
curl.exe -sS -I http://127.0.0.1:18088/tplus-sync/
docker stop aliecs-public-web-p5-test
```

Expected: HTTP 301 and `Location: http://127.0.0.1:18088/sync/?group=tplus` or nginx-equivalent relative target; container is removed in `finally`.

- [ ] **Step 7: Commit Task 5**

```powershell
git add services/public-web/exports/index.html services/public-web/nginx.conf services/public-web/health/index.html tests/test_exports_frontend.py tests/test_tplus_sync_frontend.py tests/test_health_frontend.py docs/project-navigation.md
git commit -m "feat(sync): make exports download-only"
```

---

### Task 6: Full verification, PR, deploy and live reconciliation

**Files:**
- Modify after evidence: `docs/superpowers/specs/2026-08-13-unified-sync-center-p5-design.md`
- Modify after evidence: `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: merged PR, production release and evidence without secrets.

- [ ] **Step 1: Fresh local verification**

Run and save only summaries:

```powershell
python -m unittest discover -s tests
Push-Location services/tplus-sync-worker
$env:PYTHONPATH='src;.'
python -m unittest discover -s tests
Remove-Item Env:PYTHONPATH
Pop-Location
python scripts/check_navigation.py
docker compose -f local/docker-compose.local.yml config | Out-Null
docker compose --env-file deploy/ecs/runtime.env.example -f deploy/ecs/compose.prod.yml config | Out-Null
git diff --check origin/main...HEAD
```

Also run `python -m compileall` on changed Python trees, inline JS `node --check`, nginx redirect smoke, and scan the entire diff for real doc IDs, secrets, `.env`, logs, browser data and `_references`.

- [ ] **Step 2: Critical self-review**

Compare every design requirement to code/tests. Run deterministic probes for:

- stale/later response after logout;
- combined filters and failed page/config requests;
- unknown/s3/structure source never inserts request;
- reconciliation never inserts run and disables only doc pull jobs;
- old compatibility endpoint and canonical endpoint produce the same queue effect;
- scheduler mode normalization and env examples remain shadow, with no active change in diff.

Fix every Critical/Important issue with a new failing test and separate commit.

- [ ] **Step 3: Push and open ready PR**

Before git writes, check status, branch, remote and excluded paths. Then:

```powershell
git push -u origin codex/unified-sync-center-p5
gh pr create --base main --head codex/unified-sync-center-p5 --title "feat(sync): 收口统一同步控制面" --body "统一同步中心 P5：/sync/ 接管四类资产、配置和触发；/exports/ 仅保留下载；/tplus-sync/ 重定向。`n`n验证：根 unittest、T+ unittest、PostgreSQL 16 integration、strict Node、navigation、Compose、nginx smoke。`n`n安全：API/仓库不包含外部文档 ID；s3/link/structure 来源不入队；P4 继续 shadow。`n`nNav-Impact: updated"
```

PR body includes test summaries, `Nav-Impact: updated`, source allowlist, no-ID guarantee, P4 shadow unchanged, rollback, and no production IDs/count snapshots.

- [ ] **Step 4: Gate on CI and merge**

Wait for all PR checks. The migration job must show the P5 PostgreSQL integration as one real pass with no skip. On failure, use systematic-debugging, add a regression test, push a fix and wait again. Squash merge only after all required checks pass.

- [ ] **Step 5: Deploy main**

Wait for main build, then run:

```powershell
gh workflow run release-deploy.yml --ref main -f deploy_target=business-cn
```

Wait for `stage-business-cn-peer=success`; `deploy-business-cn=skipped` is expected. Read-only SSH verifies `/srv/business-cn/current`, container image tags/digests, health and worker modes.

- [ ] **Step 6: Production UI/API acceptance**

Without printing IDs or tokens:

- curl `/sync/`, `/exports/`, `/health/` for 200 and `/tplus-sync/` for 301;
- authenticated API checks assets/overview JSON has four groups and contains no fields/values matching external ID patterns;
- confirm exports HTML has no sync/copy/config controls and four catalog groups remain downloadable;
- confirm doc/T+ scheduler env values are both exactly `shadow`.

- [ ] **Step 7: Trigger canonical production synchronization**

Call `POST /v1/sync/run-all` with the existing admin channel. Record only queued/skipped counts and numeric internal request IDs. Query DB by source type—not by copied IDs—to prove:

```sql
eligible table source ids EXCEPT enabled doc job source ids = 0
enabled doc job source ids EXCEPT eligible table source ids = 0
pending/running requests on link or structure sources = 0
synthetic P5 sync_job_runs without legacy_ref = 0
```

Wait until all newly queued requests are terminal or the documented timeout is reached. Report each failure/partial explicitly; do not retry a provider repeatedly without diagnosis.

- [ ] **Step 8: Record evidence and finish**

Update both design documents with PR number, merge SHA, CI run, deploy run, image/source evidence, dynamic coverage result, request terminal summary, P4 shadow confirmation and any remaining manual item. Commit the safe evidence on a docs branch/PR; docs-only merge requires no second deployment.
