# 统一同步平台中心 P1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让企微、飞书、T+ 全量和 T+ 父件核对四类同步作业在保留旧表写入的同时，持续写入 `sync_jobs`、`sync_job_runs` 与 `sync_job_steps`，为 P2 `/sync/` 只读页面提供真实、可追溯的运行数据。

**Architecture:** 两个 worker 的 Docker build context 彼此隔离，因此各自保留一个小型平台写入器；写入器接口一致，但不引入跨 context 的新打包层。旧 `sync_runs` / `integration_sync_runs` 始终先成功提交，再追加平台写入；平台写入失败必须回滚自己的事务并保留旧链路结果。文档同步按实际 `external_sources.id` 建动态作业，T+ 使用固定作业键 `chanjet.full` 与 `tplus.parent_match`。

**Tech Stack:** Python 3.12、psycopg 3、PostgreSQL 16、`unittest`、GitHub Actions、Docker Compose。

**设计依据：** `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md` 第 4 节、第 9 节 P1、第 10 节；P0 结构以 `db/migrations/0048_sync_job_platform.sql` 为准。

## Global Constraints

- P1 只做双写与步骤记录；旧表、旧 API、`/exports/`、`/tplus-sync/` 和 `/formula/` 的读取路径不得改变。
- 动态作业键必须为 `wecom.doc.<external_sources.id>` / `feishu.doc.<external_sources.id>`；固定作业键必须为 `chanjet.full` / `tplus.parent_match`。
- `sync_jobs` upsert 必须显式写 `updated_at = NOW()`；不得覆盖运营侧以后会编辑的 `schedule`、`freshness_sla_seconds`、`artifact_glob`、`alert_enabled`、`alert_chat_id`。
- 文档作业 `source_id` 必须引用真实 `external_sources.id`；T+ 两个固定作业的 `source_id` 为 `NULL`。
- `legacy_ref` 只能是 `{"table":"sync_runs","id":<id>}`、`{"table":"integration_sync_runs","id":<id>}` 或 `{}`；不得存 SQL、连接串、docid、chat_id 或凭据。
- 平台状态只用 `running|success|partial|failed`；旧 `partial_failed` 映射为 `partial`。
- `error_kind` 只用 `auth|rate_limit|network|schema|write|unknown`；`error_message` 必须去除 access token、corpsecret、app secret、Authorization 值并截断为 500 字符。
- 平台写入是附加可观测性，失败不得让原同步失败，也不得把已经成功的旧表事务回滚。
- AliECS 是公开仓库：不硬编码飞书群 ID，不写企微 docid，不提交 `.env`、logs、browser_data、`_references` 或任何真实凭据。
- 测试命令使用 `python -m unittest discover -s tests -p "test_xxx.py"`；T+ 子项目使用 PowerShell `$env:PYTHONPATH="src;."` 后 `python -m unittest discover -s tests -v`。
- 走独立分支 + PR，不直推 `main`；所有写 `.git` 的命令串行执行，只 `git add` 本计划明确列出的文件。

---

### Task 1: doc-sync-worker 平台写入器与错误分类

**Files:**
- Create: `services/doc-sync-worker/app/storage/sync_job_platform.py`
- Modify: `services/doc-sync-worker/app/storage/postgres.py`（`PostgresDocSyncStore.__init__` / `close`）
- Create: `tests/test_doc_sync_job_platform.py`

**Interfaces:**
- Consumes: P0 表 `sync_jobs`、`sync_job_runs`、`sync_job_steps`
- Produces: `SyncJobPlatformWriter.start_run`、`upsert_step`、`finish_run`、`open_owned`、`close`，以及 `platform_writer_for(store)`、`classify_error(exc)`、`safe_error_message(exc)`

- [ ] **Step 1: 写失败的纯单元测试**

在 `tests/test_doc_sync_job_platform.py` 用记录 SQL 的 fake connection 覆盖以下行为：

文件顶部按既有 `tests/test_doc_sync_worker.py` 的方式把 `services/doc-sync-worker` 的绝对路径插到 `sys.path` 首位，再导入 `app.storage.sync_job_platform`；不得依赖调用者预设 `PYTHONPATH`。

```python
def test_job_upsert_refreshes_updated_at_without_overwriting_operator_fields(self):
    writer = SyncJobPlatformWriter(self.conn)
    writer.start_run(
        job_key="wecom.doc.17", kind="pull", provider="wecom",
        display_name="点检表", source_id=17, trigger="manual",
        legacy_ref={"table": "sync_runs", "id": 91},
    )
    sql = "\n".join(self.conn.sql)
    self.assertIn("updated_at = NOW()", sql)
    for protected in ("schedule =", "freshness_sla_seconds =", "artifact_glob =",
                      "alert_enabled =", "alert_chat_id ="):
        self.assertNotIn(protected, sql)

def test_step_uses_the_unique_run_seq_conflict_target(self):
    SyncJobPlatformWriter(self.conn).upsert_step(31, 2, "fetch_page", "success", items=40)
    self.assertIn("ON CONFLICT (run_id, seq)", "\n".join(self.conn.sql))

def test_platform_write_failure_rolls_back_and_returns_none(self):
    conn = FailingConn()
    result = SyncJobPlatformWriter(conn).start_run(
        job_key="wecom.doc.17", kind="pull", provider="wecom",
        display_name="点检表", source_id=17, trigger="manual", legacy_ref={},
    )
    self.assertIsNone(result)
    self.assertEqual(1, conn.rollback_count)

def test_error_classifier_and_redaction(self):
    self.assertEqual("auth", classify_error(RuntimeError("access_token expired")))
    self.assertEqual("rate_limit", classify_error(RuntimeError("HTTP 429 too many requests")))
    self.assertEqual("network", classify_error(TimeoutError("read timed out")))
    safe = safe_error_message(RuntimeError("Authorization: Bearer secret-value"))
    self.assertNotIn("secret-value", safe)
    self.assertLessEqual(len(safe), 500)
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_doc_sync_job_platform.py" -v`

Expected: FAIL，原因是 `app.storage.sync_job_platform` 尚不存在；不得接受 import 路径错误之外的失败。

- [ ] **Step 3: 实现最小写入器**

`sync_job_platform.py` 必须提供这些完整接口：构造器接收 `(conn, logger=print, owns_connection=False)`；`start_run` 接收 keyword-only 的 `job_key, kind, provider, display_name, source_id, trigger, legacy_ref` 并返回 `int | None`；`upsert_step` 接收 `run_id, seq, name, status` 以及 keyword-only 的 `items=0, message=""`；`finish_run` 接收 `run_id` 以及 keyword-only 的 `status, row_count, changed_count, error, detail_json`。`open_owned(logger=print)` 用本模块的 `connect()` 创建 owned writer；`close()` 只关闭 owned connection。`platform_writer_for(store)` 在生产返回 `store.sync_jobs`，旧 FakeStore 没有该属性时返回同接口的 no-op writer。

`start_run` 的作业 upsert 只更新平台拥有的目录字段：

```sql
INSERT INTO sync_jobs(job_key, kind, provider, display_name, source_id, updated_at)
VALUES (%s, %s, %s, %s, %s, NOW())
ON CONFLICT(job_key) DO UPDATE SET
    kind = EXCLUDED.kind,
    provider = EXCLUDED.provider,
    display_name = EXCLUDED.display_name,
    source_id = EXCLUDED.source_id,
    updated_at = NOW()
RETURNING id
```

随后插入 `sync_job_runs(job_id, trigger, status, legacy_ref)`。`upsert_step` 必须使用 `(run_id, seq)` 冲突目标，首次 `running` 写 `started_at=NOW()`，终态 `success|failed` 写 `finished_at=NOW()`。三个写方法都用同一个 `_best_effort()` 包裹：异常时 `conn.rollback()`、输出不含参数值的短日志，并返回 `None`/不抛异常。

`PostgresDocSyncStore.__init__` 增加 `self.sync_jobs = SyncJobPlatformWriter(conn)`；不得打开第二条数据库连接。

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `python -m unittest discover -s tests -p "test_doc_sync_job_platform.py" -v`

Expected: 新文件全部通过。

Run: `python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v`

Expected: 既有 doc worker 测试全部通过。

- [ ] **Step 5: 提交**

```powershell
git add -- services/doc-sync-worker/app/storage/sync_job_platform.py services/doc-sync-worker/app/storage/postgres.py tests/test_doc_sync_job_platform.py
git commit -m "feat(sync): add doc worker platform run writer"
```

---

### Task 2: 企微作业双写 runs/steps

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_wecom_full.py`
- Modify: `tests/test_doc_sync_worker.py`

**Interfaces:**
- Consumes: Task 1 的 `store.sync_jobs`
- Produces: `wecom.doc.<source_id>` 作业；步骤顺序 `token` → `list_sheets` → `fetch_page` → `normalize` → `upsert`

- [ ] **Step 1: 写失败的企微行为测试**

在 `tests/test_doc_sync_worker.py` 新增记录型 `FakePlatformWriter`，覆盖三条路径：

```python
def test_wecom_manual_source_writes_running_steps_and_success(self):
    status, legacy_run_id, _detail = sync_wecom_source(store, source_id=17, mode="manual")
    self.assertEqual("success", status)
    self.assertEqual(42, legacy_run_id)
    self.assertEqual("wecom.doc.17", store.sync_jobs.started[0]["job_key"])
    self.assertEqual("manual", store.sync_jobs.started[0]["trigger"])
    self.assertEqual({"table": "sync_runs", "id": 42}, store.sync_jobs.started[0]["legacy_ref"])
    self.assertEqual(["token", "fetch_page", "normalize", "upsert"], store.sync_jobs.successful_step_names())
    self.assertEqual("success", store.sync_jobs.finished[0]["status"])

def test_wecom_failure_finishes_platform_run_without_hiding_legacy_failure(self):
    status, legacy_run_id, _detail = sync_wecom_source(failing_store, source_id=17, mode="manual")
    self.assertEqual("failed", status)
    self.assertEqual(42, legacy_run_id)
    self.assertEqual("failed", failing_store.sync_jobs.finished[0]["status"])
    self.assertEqual("auth", failing_store.sync_jobs.finished[0]["error_kind"])

def test_wecom_platform_writer_failure_does_not_change_legacy_result(self):
    store.sync_jobs = RaisingPlatformWriter()
    status, legacy_run_id, _detail = sync_wecom_source(store, source_id=17, mode="manual")
    self.assertEqual(("success", 42), (status, legacy_run_id))
    self.assertEqual("success", store.finished["status"])
```

- [ ] **Step 2: 跑企微测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v`

Expected: 新断言因没有 `store.sync_jobs` 调用而失败；既有断言保持通过。

- [ ] **Step 3: 接入企微生命周期**

保留现有 `store.start_run(provider, env_profile, mode)` / `store.finish_run(run_id, status, counts, error_json)` 的位置和返回语义。只有旧 `start_run` 返回 legacy id 后，才调用：

```python
platform_run_id = store.sync_jobs.start_run(
    job_key=f"wecom.doc.{source_id}", kind="pull", provider="wecom",
    display_name=str(source.get("source_name") or source_id), source_id=source_id,
    trigger="manual" if mode == "manual" else "schedule",
    legacy_ref={"table": "sync_runs", "id": run_id},
)
```

表级手动同步写 `token/fetch_page/normalize/upsert`；doc 级同步在 `list_sheets` 后为每个表写 `fetch_page/normalize/upsert`，`message` 写表名，`items` 写该步真实条目数。定时全量在已获得 `external_sources.id` 后按相同键记录；同一 legacy profile run 可以被多个平台作业的 `legacy_ref` 引用。

异常发生时先按原逻辑追加 `errors`、决定 legacy status，执行 `store.finish_run(run_id, status=status, counts=counts, error_json=errors)`；随后把平台当前步骤置 `failed` 并以 `status="failed"` 完成平台 run。平台 writer 自己的异常再由一层 `try/except` 隔离，确保 Fake/未来替换实现也不能破坏旧链路。

平台成功统计使用：`row_count=counts["record_count"]`，`changed_count=created_count+updated_count`；旧 `partial_failed` 写平台 `partial`。

- [ ] **Step 4: 验证企微路径**

Run: `python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v`

Expected: 全部通过；新测试同时证明 manual trigger、legacy_ref、步骤顺序和 fail-open。

- [ ] **Step 5: 提交**

```powershell
git add -- services/doc-sync-worker/app/pipelines/sync_wecom_full.py tests/test_doc_sync_worker.py
git commit -m "feat(sync): dual-write WeCom job runs and steps"
```

---

### Task 3: 飞书作业双写 runs/steps

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`
- Modify: `tests/test_doc_sync_worker.py`

**Interfaces:**
- Consumes: Task 1 的 `store.sync_jobs`
- Produces: `feishu.doc.<source_id>` 作业；步骤顺序 `token` → `list_sheets` → `fetch_page` → `normalize` → `upsert`

- [ ] **Step 1: 写失败的飞书行为测试**

在现有 `FeishuManualSyncTests` 的 FakeStore 增加 `sync_jobs` 记录器，并新增：

```python
def test_feishu_table_request_dual_writes_with_legacy_ref(self):
    store, (status, run_id, _detail) = self._run(self._table_source())
    self.assertEqual(("success", 42), (status, run_id))
    self.assertEqual("feishu.doc.9", store.sync_jobs.started[0]["job_key"])
    self.assertEqual({"table": "sync_runs", "id": 42}, store.sync_jobs.started[0]["legacy_ref"])
    self.assertEqual(["token", "fetch_page", "normalize", "upsert"], store.sync_jobs.successful_step_names())

def test_feishu_partial_maps_to_platform_partial(self):
    store, (status, _run_id, _detail) = self._run_doc_with_one_failed_table()
    self.assertEqual("partial_failed", status)
    self.assertEqual("partial", store.sync_jobs.finished[-1]["status"])
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v`

Expected: 新平台断言失败，既有飞书整簿重扫与单表同步测试仍绿。

- [ ] **Step 3: 接入飞书生命周期**

复用 Task 2 的规则，不复制新的状态映射器。`sync_feishu_source` 在旧 legacy run 创建后启动平台 run；整簿路径为每个 `_rescan_app_tables` 返回的 `table_source_id` 建作业，表级路径只建一个作业。`_sync_bitable_records` 把 `records_response["page_count"]` 写入 `fetch_page.message`，把记录数写入 `items`。

任何单表异常必须完成该表对应的平台 failed run 后继续其他表；整簿最终 legacy `partial_failed` 保持原样。凭据、app_token 和 Authorization 不得进入 `detail_json` 或 `error_message`。

- [ ] **Step 4: 验证飞书与企微共存**

Run: `python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v`

Expected: 全部通过；企微与飞书新测试同时为绿。

- [ ] **Step 5: 提交**

```powershell
git add -- services/doc-sync-worker/app/pipelines/sync_feishu_full.py tests/test_doc_sync_worker.py
git commit -m "feat(sync): dual-write Feishu job runs and steps"
```

---

### Task 4: tplus-sync-worker 平台写入器与 CI 测试入口

**Files:**
- Create: `services/tplus-sync-worker/src/tplus_datahub/jobs/sync_job_platform.py`
- Create: `services/tplus-sync-worker/tests/test_sync_job_platform.py`
- Modify: `.github/workflows/ci.yml`（validate job 的 Python 单元测试步骤）

**Interfaces:**
- Consumes: `DATABASE_URL` 与 P0 三张运行表
- Produces: 模块函数 `start_run`、`upsert_step`、`finish_run`、`attach_legacy_ref`；无 DB 配置时全部 no-op

- [ ] **Step 1: 写失败的 T+ writer 测试**

测试固定作业 `source_id IS NULL`、`updated_at=NOW()`、步骤 upsert、legacy_ref 后挂接，以及 `ChanjetAPIError(status_code=401/429/500)` 分别映射 `auth/rate_limit/network`。测试还要让 fake connect 抛错，断言调用返回 `None` 且不向外抛。

- [ ] **Step 2: 跑 T+ 测试确认 RED**

Run:

```powershell
Push-Location services/tplus-sync-worker
$env:PYTHONPATH="src;."
python -m unittest discover -s tests -p "test_sync_job_platform.py" -v
Pop-Location
```

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 实现 T+ writer**

接口与 Task 1 同语义：`start_run` 接收 keyword-only 的 `job_key, kind, provider, display_name, source_id, trigger, legacy_ref` 并返回 `int | None`；`upsert_step` 接收 `run_id, seq, name, status, items=0, message=""`；`finish_run` 接收 `run_id, status, row_count, changed_count, error, detail_json`；`attach_legacy_ref` 接收 `platform_run_id, legacy_run_id`。每个函数都通过 `connect_if_configured()` 获取短连接并在 `with closing(conn)` 内提交/回滚；不得持有跨同步周期的连接。固定作业的 job upsert 传 `source_id=None`，且同样不更新运营字段。

`attach_legacy_ref(platform_run_id, legacy_run_id)` 只执行：

```sql
UPDATE sync_job_runs
SET legacy_ref = %s
WHERE id = %s
```

值固定为 `{"table":"integration_sync_runs","id":legacy_run_id}`。

- [ ] **Step 4: 把 T+ 子项目测试纳入 CI**

在 `.github/workflows/ci.yml` 的 Python 单元测试步骤末尾追加：

```bash
(cd services/tplus-sync-worker && PYTHONPATH=src:. python -m unittest discover -s tests -v)
```

这一步只启用仓内现有测试，不新增依赖。

- [ ] **Step 5: 验证并提交**

Run: 上述 T+ 测试命令。

Run: `python -m unittest discover -s tests -p "test_doc_sync_job_platform.py" -v`

Run: `python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text(encoding='utf-8')); print('workflow_yaml_ok')"`

Expected: 全绿并输出 `workflow_yaml_ok`。

```powershell
git add -- services/tplus-sync-worker/src/tplus_datahub/jobs/sync_job_platform.py services/tplus-sync-worker/tests/test_sync_job_platform.py .github/workflows/ci.yml
git commit -m "feat(sync): add T+ platform run writer"
```

---

### Task 5: T+ 全量作业双写与模块步骤

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_all.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/worker_loop.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/db_sync_requests.py`
- Modify: `services/tplus-sync-worker/tests/test_worker_loop.py`
- Modify: `services/tplus-sync-worker/tests/test_db_sync_requests.py`
- Create: `services/tplus-sync-worker/tests/test_job_sync_all_platform.py`

**Interfaces:**
- Consumes: Task 4 writer
- Produces: `chanjet.full` 作业；每个实际模块一个步骤；`SyncAllResult.platform_run_id: int | None`

- [ ] **Step 1: 写失败的生命周期测试**

新增测试证明：

```python
def test_sync_all_records_each_module_and_partial_result(self):
    result = job_sync_all.run(trigger="schedule", platform=platform)
    self.assertEqual("chanjet.full", platform.started[0]["job_key"])
    self.assertEqual("schedule", platform.started[0]["trigger"])
    self.assertIn("bom", platform.successful_step_names())
    self.assertIn("inventory", platform.failed_step_names())
    self.assertEqual("partial", platform.finished[0]["status"])

def test_worker_attaches_scheduled_legacy_run(self):
    run_forever(sync_once=lambda: outcome_with_platform_id(77), record_sync_run=record, max_runs=1)
    self.assertEqual(77, record.calls[0]["platform_run_id"])

def test_manual_full_attaches_legacy_run_without_becoming_scheduled(self):
    finish_full_request(9, "success", 0, {"platform_run_id": 77})
    self.assertIn("'manual_full'", self.joined_sql)
    self.assertEqual((77, self.legacy_id), self.attached)
```

- [ ] **Step 2: 跑 T+ 全套确认 RED**

Run:

```powershell
Push-Location services/tplus-sync-worker
$env:PYTHONPATH="src;."
python -m unittest discover -s tests -v
Pop-Location
```

Expected: 新测试因 `trigger/platform/platform_run_id` 接口不存在而失败。

- [ ] **Step 3: 在 job_sync_all 内记录模块步骤**

把签名改为：

```python
def run(*, trigger: str = "manual", platform: Any | None = None) -> SyncAllResult:
```

未注入 `platform` 时使用 Task 4 模块；开始即创建 `chanjet.full` running run。`stage(module_name, action)` 在 action 前写 running，成功写 success；异常写 failed，message 只放清洗后的异常。最终 `failed_modules` 非空即平台 `partial`，配置无法加载或顶层未知异常为 `failed`，否则 `success`。`detail_json` 保存 `export_files`、`diff_summary`、`full_snapshot_id`、`failed_modules`，同时保留现有 `SyncAllResult` 旧字段。

- [ ] **Step 4: 连接 scheduled/manual 与旧 run**

`worker_loop.run_forever` 默认 scheduled 调用显式使用 `trigger="schedule"`；手动全量请求调用显式使用 `trigger="manual"`。测试注入的无参 `sync_once` 仍按原方式调用，不强迫测试 lambda 接收新参数。

`record_tplus_sync_run_if_configured` 新增 keyword 参数 `platform_run_id: int | None = None`，在 legacy INSERT 成功并 commit 后调用 `attach_legacy_ref`。`finish_full_request` 从 `detail["platform_run_id"]` 取得 id，在 legacy INSERT 与请求 UPDATE commit 后挂接。任何挂接失败只记录日志，不改变 legacy 结果。

- [ ] **Step 5: 验证并提交**

Run: T+ 全套命令。

Expected: 全部通过；既有锚点、手动全量、BOM 请求和产出物断言无回归。

```powershell
git add -- services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_all.py services/tplus-sync-worker/src/tplus_datahub/jobs/worker_loop.py services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py services/tplus-sync-worker/src/tplus_datahub/jobs/db_sync_requests.py services/tplus-sync-worker/tests/test_worker_loop.py services/tplus-sync-worker/tests/test_db_sync_requests.py services/tplus-sync-worker/tests/test_job_sync_all_platform.py
git commit -m "feat(sync): dual-write T+ full runs and module steps"
```

---

### Task 6: T+ 父件核对作业双写

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/tplus_parent_match.py`
- Modify: `services/doc-sync-worker/app/pipelines/worker_loop.py`
- Modify: `tests/test_tplus_parent_match.py`

**Interfaces:**
- Consumes: Task 1 writer
- Produces: 固定作业 `tplus.parent_match`，`kind="reconcile"`；步骤 `load_source` → `fetch_page` → `normalize` → `writeback` → `notify`

- [ ] **Step 1: 写失败的父件核对测试**

扩展现有 `_stub_run_dependencies`，注入 fake platform writer，并覆盖：正常写 `success`、企微读取失败写 `failed/network`、批次写入部分失败写 `partial/write`、`dry_run=True` 仍产生 run 但 `writeback.message="dry-run"`、`notify=False` 的 notify 步骤为 success 且 message 为 `disabled`。

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_tplus_parent_match.py" -v`

Expected: 新的 `tplus.parent_match` 生命周期断言失败；原有 40+ 行为测试保持通过。

- [ ] **Step 3: 接入固定作业生命周期**

把签名扩展为：

```python
def run_tplus_parent_match(*, dry_run: bool = False, notify: bool = True,
                           trigger: str = "manual", platform: Any | None = None) -> int:
```

未注入时通过 Task 1 的 `SyncJobPlatformWriter` 打开一条 owned connection，并在 `finally` 关闭。固定参数：`job_key="tplus.parent_match"`、`kind="reconcile"`、`provider="chanjet"`、`display_name="T+ 父件核对"`、`source_id=None`、`legacy_ref={}`。

`worker_loop._default_full_sync` 调用 `trigger="schedule"`；`run_backfill_if_bom_synced` 调用 `trigger="event"`；CLI 保持默认 `manual`。原有飞书业务核对结果推送不改变，P3 才接管“自身失败”的统一告警。

- [ ] **Step 4: 验证并提交**

Run: `python -m unittest discover -s tests -p "test_tplus_parent_match.py" -v`

Run: `python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v`

Expected: 全绿。

```powershell
git add -- services/doc-sync-worker/app/pipelines/tplus_parent_match.py services/doc-sync-worker/app/pipelines/worker_loop.py tests/test_tplus_parent_match.py
git commit -m "feat(sync): record T+ parent match runs and steps"
```

---

### Task 7: PostgreSQL 集成验证、导航与 P1 记录

**Files:**
- Create: `tests/test_sync_job_platform_integration.py`
- Modify: `.github/workflows/ci.yml`（migration-dry-run job）
- Modify: `docs/project-ai-map.md`

**Interfaces:**
- Consumes: Task 1–6 全部实现
- Produces: CI 中真实 PostgreSQL 双写证据与更新后的 AI 导航

- [ ] **Step 1: 写 PostgreSQL 集成测试**

测试文件顶部把 `services/doc-sync-worker` 与 `services/tplus-sync-worker/src` 的绝对路径插到 `sys.path`。测试仅在 `SYNC_JOB_PLATFORM_INTEGRATION=1` 时执行，否则 `skipTest`。它必须连接 CI Postgres，插入唯一测试 `external_sources`，用 doc writer 完成 running → steps → success，再用 T+ writer完成 running → failed → legacy_ref 挂接，并断言：

```sql
SELECT job_key, source_id, updated_at FROM sync_jobs WHERE job_key LIKE 'ci.%';
SELECT trigger, status, legacy_ref FROM sync_job_runs WHERE job_id = %s ORDER BY id;
SELECT seq, name, status, items FROM sync_job_steps WHERE run_id = %s ORDER BY seq;
```

测试结束删除 `job_key LIKE 'ci.%'` 的作业与测试 external source；不得访问外部 provider。

- [ ] **Step 2: 在 migration-dry-run 真实运行集成测试**

在迁移应用之后、backend smoke 之前增加：

```yaml
      - name: Run sync job platform integration test
        env:
          DATABASE_URL: postgresql://aliecs:aliecs@127.0.0.1:5432/aliecs
          SYNC_JOB_PLATFORM_INTEGRATION: "1"
        run: python -m unittest discover -s tests -p "test_sync_job_platform_integration.py" -v
```

- [ ] **Step 3: 更新导航和 spec**

`docs/project-ai-map.md` 的 doc-sync-worker 输出表补充 `sync_jobs/runs/steps`，列出 `app/storage/sync_job_platform.py`；T+ 段列出 `jobs/sync_job_platform.py`，并把“CI 不覆盖”改为“根 CI 同时运行子项目 unittest”。spec 的 P1 实施行依赖真实 PR URL，统一留到 Task 8 开 PR 后填写。

- [ ] **Step 4: 跑完整验证**

Run: `python -m unittest discover -s tests`

Run:

```powershell
Push-Location services/tplus-sync-worker
$env:PYTHONPATH="src;."
python -m unittest discover -s tests -v
Pop-Location
```

Run: `python scripts/check_navigation.py`

Run: `docker compose -f local/docker-compose.local.yml config`

Expected: 两套测试全绿、导航通过、Compose config exit 0。

- [ ] **Step 5: 提交测试与导航，spec PR 号留到 Task 8**

```powershell
git add -- tests/test_sync_job_platform_integration.py .github/workflows/ci.yml docs/project-ai-map.md
git commit -m "test(sync): verify P1 dual writes against PostgreSQL"
```

---

### Task 8: 终审、PR、部署与生产四类证据

**Files:**
- Modify: `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md`
- Update: `.superpowers/sdd/2026-08-12-unified-sync-center-p1/progress.md`（git-ignored ledger，只作执行证据，不提交）

**Interfaces:**
- Consumes: Task 1–7 的完整分支
- Produces: 已合并、已部署且可供 P2 读取的 P1 生产事实

- [ ] **Step 1: 按 SDD 做全分支终审与最终验证**

终审范围为 merge-base 到 HEAD；Critical/Important 必须修复并复审。随后重新运行 Task 7 的两套全量测试、导航和 Compose config，记录实际数量与 exit code。

- [ ] **Step 2: 提交前安全检查并推送**

串行执行 `git status --short`、`git branch --show-current`、`git remote get-url origin`、`git diff --name-only origin/main...HEAD`。确认 `.env`、logs、browser_data、`_references`、`.superpowers/` 不在 diff 后，仅显式暂存本任务 spec 文件并提交；其他代码应已由前七个任务分别提交。

推送 `codex/unified-sync-center-p1`，创建 ready PR，正文必须含：变更、双写 fail-open 边界、两套测试、真实 PostgreSQL 集成验证、`Nav-Impact: updated`。

- [ ] **Step 3: 回填真实 PR 号并等待 CI**

用 PR 实际编号和 URL 更新 spec 第 12 节，显式 `git add` 该 spec、提交、推送。运行 `gh pr checks --watch`；只有 `validate`、`migration-dry-run`、PR 正文检查全绿才 squash 合并。

- [ ] **Step 4: 手工触发 business-cn 发布并确认 job**

Run: `gh workflow run release-deploy.yml --ref main -f deploy_target=business-cn`

触发前记录 UTC 时间；触发后用实际 workflow_dispatch 结果取 run id 并确认：

```powershell
$runId = gh run list --workflow release-deploy.yml --event workflow_dispatch --branch main --limit 1 --json databaseId --jq '.[0].databaseId'
gh run view $runId --json jobs --jq '.jobs[] | select(.name|test("stage-business-cn-peer|deploy-business-cn")) | [.name,.conclusion] | @tsv'
```

Expected: `stage-business-cn-peer success`；`deploy-business-cn skipped` 为正常。

- [ ] **Step 5: txecs 容器与安全触发验证**

Run:

```bash
ssh txecs "sudo docker ps --format '{{.Names}}\t{{.Status}}\t{{.Image}}' | grep -E 'business-cn-(doc-sync-worker|tplus-sync-worker)-1'"
ssh txecs "sudo docker exec business-cn-doc-sync-worker-1 python -m app.main tplus-parent-match --dry-run --no-notify"
```

Expected: 两个 worker 都是本次新镜像且启动时间刷新；父件核对 dry-run exit 0，不写企微、不推飞书，但产生 `tplus.parent_match` 的 manual run/steps。

- [ ] **Step 6: 查询四类平台数据与 legacy_ref**

通过生产 postgres 容器执行只读 SQL：

```sql
SELECT job_key, kind, provider, source_id, updated_at
FROM sync_jobs
WHERE job_key LIKE 'wecom.doc.%'
   OR job_key LIKE 'feishu.doc.%'
   OR job_key IN ('chanjet.full', 'tplus.parent_match')
ORDER BY job_key;

SELECT j.job_key, r.id, r.trigger, r.status, r.legacy_ref,
       count(s.id) AS step_count
FROM sync_job_runs r
JOIN sync_jobs j ON j.id = r.job_id
LEFT JOIN sync_job_steps s ON s.run_id = r.id
GROUP BY j.job_key, r.id
ORDER BY r.id DESC
LIMIT 30;
```

对 `legacy_ref.table='sync_runs'` 与 `integration_sync_runs` 各抽一条 id 回查旧表存在。若某类作业尚未自然执行，只允许使用现有 worker CLI/现有手动请求入口触发一轮；不得直接向 `sync_job_*` 伪造生产证据。

- [ ] **Step 7: 完成 ledger 与阶段结论**

在 P1 ledger 记录 PR、merge SHA、workflow run/attempt、两个 worker 镜像 tag/digest、四类作业 SQL 摘要、legacy_ref 回查与仍存在的 minor。只有上述证据齐全才把 P1 标记完成并进入 P2 writing-plans。

---

## 阶段完成判据

1. 根测试与 T+ 子项目测试全绿，CI 已真实运行两套测试。
2. PostgreSQL 集成测试证明 job/run/step upsert 与 legacy_ref 可用。
3. 旧 `sync_runs` / `integration_sync_runs` 写入与现有 API 行为不变；平台写入故障反例证明 fail-open。
4. PR 已 squash 合并，`stage-business-cn-peer` success，txecs 两个 worker 已换新镜像。
5. 生产四类 job 都有真实 run/steps；文档与 T+ 全量至少各一条 legacy_ref 能回查旧表。
6. spec 第 12 节与 P1 ledger 已写明实际 PR、部署和验证证据。

## 明确不在 P1 范围内

- 不建 `/v1/sync/*` API，不建 `/sync/` 页面。
- 不写 notifier，不碰 `ops.py` 两个告警 loop。
- 不搬调度配置，不新增 `SYNC_SCHEDULER_MODE`，不做 P4 shadow/active。
- 不重定向 `/tplus-sync/`，不瘦身 `/exports/`。
- 不改业务数据表、formula 读取链路、T+ 写回开关或生产密钥。
