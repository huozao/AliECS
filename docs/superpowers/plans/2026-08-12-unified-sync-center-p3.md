# 统一同步平台中心 P3 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在唯一的 `doc-sync-worker` 中上线统一告警 notifier，覆盖失败、升级、SLA、产出物、T+ openToken 与恢复通知，并在接管验证完成后删除 backend 的两条旧告警线程。

**Architecture:** notifier 由纯判定层、PostgreSQL 状态机与飞书发送适配器组成，挂入 doc-sync-worker 现有 30 秒 poll；`sync_job_alerts` 是唯一告警状态源，partial unique index 负责首次抢占，行锁负责升级/恢复的并发互斥。T+ 全量继续用既有 `chanjet.full` 作业，补齐 48 小时 SLA、只读产出物 glob 和 run 级 artifact 元数据；backend 仅保留只读 API，不再调外部告警 API。

**Tech Stack:** Python 3 `unittest`、psycopg 3/PostgreSQL 16、飞书开放平台文本消息、Docker Compose、GitHub Actions。

**设计依据：** `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md` 第 4、6、8、9、10 节及 `P3 接手须知`。

## Global Constraints

- notifier 必须位于 `services/doc-sync-worker/app/pipelines/sync_alert_notifier.py`，不得放回多副本 backend；backend 只能查询同步结果、状态和日志。
- `sync_job_alerts` 是唯一告警状态表；不得新增第二套状态或在页面伪造 legacy 告警。
- 首次告警必须使用迁移注释写死的冲突目标：`ON CONFLICT (job_id, alert_kind) WHERE state = 'open' DO NOTHING RETURNING id`；不得省略谓词，也不得改成无 target 的 `ON CONFLICT DO NOTHING`。
- 告警状态字面量只能是小写 `open` / `resolved`；只有抢占到新行的执行者可以推首次告警。
- 默认升级间隔为 21600 秒（6 小时）；产出物允许 300 秒写文件/提交 run 的时差，只有 `max(mtime) + 300 < last_success.started_at` 才算 `artifact_stale`。
- T+ openToken 判据沿用旧实现：JWT 有效期 6 天，剩余少于 345600 秒（4 天）即 `credential_expiring`；刷新后必须恢复。
- `chanjet.full` 必须用 `COALESCE` 补齐 `freshness_sla_seconds=172800` 与 `artifact_glob='/app/tplus-output/excel/*.xlsx'`；不得覆盖运营侧已填写的值。
- 成功 run 的 steps 保留 30 天；非成功 run 的 steps 保留 90 天。只清 `sync_job_steps`，不删 run、alert 或业务数据。
- 全局群只读 env `SYNC_ALERT_CHAT_ID`；每作业 `sync_jobs.alert_chat_id` 可覆盖。公开仓不得硬编码真实 chat id、token、docid 或 secret。
- 飞书发送失败不得改变同步任务结果；不得在日志、payload、消息或 API 中写入 token、app secret、external docid 或绝对产出路径。
- `tplus_parent_match.py` 的业务核对结果推送保留；它自身失败继续由 P1 的 `tplus.parent_match` failed run 交给 notifier 报警。
- P3 同批删除 `ops.py` 的 `_chanjet_token_alert_loop` 与 `_tplus_full_sync_alert_loop`，但实现/测试顺序必须先证明 notifier 接管，再删除旧线程；不得先删后补。
- `/exports/`、`/tplus-sync/`、`/formula/` 与 P2 的 GET-only `/v1/sync/*` 行为保持不变；P5 范围本阶段不碰。
- 测试命令统一使用 `python -m unittest discover -s tests -p "test_xxx.py"`；T+ 子项目在 `services/tplus-sync-worker` 下用 `PYTHONPATH=src`。
- AliECS 走 `codex/` 分支 + ready PR；全部 git 写操作串行，显式 `git add -- <files>`，不得 `git add -A`。

---

### Task 1: notifier 纯判定、消息与 token 状态

**Files:**
- Create: `services/doc-sync-worker/app/pipelines/sync_alert_notifier.py`
- Create: `tests/test_sync_alert_notifier.py`

**Interfaces:**
- Produces: `credential_status(token_path: str, *, now: datetime) -> dict[str, Any]`
- Produces: `artifact_is_stale(last_success_started_at, artifacts, *, grace_seconds=300) -> bool`
- Produces: `build_alert_text(event: str, alert: dict[str, Any], *, now: datetime) -> str`
- Produces: `error_kind_label(error_kind: str | None) -> str`
- Later tasks extend the same module with `SyncAlertRepository`, `send_feishu_text` and `run_notifier_once`.

- [ ] **Step 1: 写失败的纯函数测试**

创建测试，至少锁定以下边界：

```python
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)

def test_credential_threshold_and_recovery(self):
    exp = int((NOW + timedelta(days=4)).timestamp())
    token = make_unsigned_jwt({"exp": exp})
    with token_file(token) as path:
        self.assertFalse(notifier.credential_status(path, now=NOW)["ok"])
    exp = int((NOW + timedelta(days=4, seconds=1)).timestamp())
    with token_file(make_unsigned_jwt({"exp": exp})) as path:
        self.assertTrue(notifier.credential_status(path, now=NOW)["ok"])

def test_artifact_requires_a_material_gap(self):
    started = NOW
    self.assertFalse(notifier.artifact_is_stale(started, [{"mtime_epoch": started.timestamp() - 300}]))
    self.assertTrue(notifier.artifact_is_stale(started, [{"mtime_epoch": started.timestamp() - 301}]))

def test_failed_message_is_classified_and_links_to_job(self):
    text = notifier.build_alert_text("open", {
        "alert_kind": "failed", "job_key": "wecom.doc.17", "display_name": "企微·点检表",
        "error_kind": "auth", "last_success_at": NOW - timedelta(hours=32),
        "consecutive_failures": 3,
    }, now=NOW)
    self.assertIn("同步失败：企微·点检表", text)
    self.assertIn("凭据过期(auth)", text)
    self.assertIn("连续失败 3 次", text)
    self.assertIn("https://hydwang.xyz/sync/?job=wecom.doc.17", text)
```

同时覆盖：token 文件缺失/空/JWT 非法/已过期；无 artifact 与最新 artifact；`rate_limit/network/schema/write/unknown` 中文短语；`open/escalate/resolved` 三种标题；消息不得出现 `external_doc_id`、token、secret 或 traceback。

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_sync_alert_notifier.py" -v`

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 实现最小纯函数**

实现固定词表和边界：

```python
ERROR_KIND_LABELS = {
    "auth": "凭据过期", "rate_limit": "请求限流", "network": "网络异常",
    "schema": "数据结构变化", "write": "写入失败", "unknown": "未知错误",
}
TOKEN_ALERT_THRESHOLD_SECONDS = 4 * 86400
ARTIFACT_GRACE_SECONDS = 300

def artifact_is_stale(last_success_started_at, artifacts, *, grace_seconds=ARTIFACT_GRACE_SECONDS):
    mtimes = [float(item["mtime_epoch"]) for item in artifacts if item.get("mtime_epoch") is not None]
    if not mtimes or last_success_started_at is None:
        return True
    return max(mtimes) + grace_seconds < last_success_started_at.timestamp()
```

`credential_status` 只返回配置/健康/过期/到期时间/剩余小时/脱敏消息，不返回 token；`build_alert_text` 只用平台元数据并对 job key 做 URL 编码。

- [ ] **Step 4: 验证纯函数**

Run: `python -m unittest discover -s tests -p "test_sync_alert_notifier.py" -v`

Expected: 全绿。

- [ ] **Step 5: 提交**

```powershell
git add -- services/doc-sync-worker/app/pipelines/sync_alert_notifier.py tests/test_sync_alert_notifier.py
git commit -m "feat(sync): add notifier alert decisions"
```

---

### Task 2: PostgreSQL 告警状态机、升级互斥与保留清理

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_alert_notifier.py`
- Modify: `tests/test_sync_alert_notifier.py`

**Interfaces:**
- Produces: `SyncAlertRepository(conn, *, now_fn, escalation_seconds=21600)`
- Produces: `load_job_states() -> list[dict[str, Any]]`
- Produces: `claim_alert(job, run_id, alert_kind, payload) -> int | None`
- Produces: `deliver_due(alert_id, sender) -> bool`
- Produces: `resolve_alert(alert_id, payload, sender) -> bool`
- Produces: `cleanup_steps() -> int`

- [ ] **Step 1: 写失败的 SQL 与状态转换测试**

用记录 SQL/参数/commit/rollback 的 fake connection 覆盖：

```python
def test_claim_uses_exact_partial_index_inference(self):
    alert_id = self.repo.claim_alert(self.job, 31, "failed", {"status": "failed"})
    sql = self.conn.joined_sql()
    self.assertIn("ON CONFLICT (job_id, alert_kind) WHERE state = 'open' DO NOTHING", sql)
    self.assertIn("RETURNING id", sql)
    self.assertNotIn("ON CONFLICT DO NOTHING", sql)
    self.assertEqual(91, alert_id)

def test_delivery_holds_row_lock_and_only_marks_successful_send(self):
    self.assertTrue(self.repo.deliver_due(91, lambda alert: True))
    self.assertIn("FOR UPDATE SKIP LOCKED", self.conn.joined_sql())
    self.assertIn("notify_count = notify_count + 1", self.conn.joined_sql())

def test_failed_delivery_rolls_back_for_next_poll(self):
    self.assertFalse(self.repo.deliver_due(91, lambda alert: False))
    self.assertEqual(1, self.conn.rollback_count)
    self.assertNotIn("notify_count = notify_count + 1", self.conn.committed_sql())

def test_cleanup_uses_30_and_90_day_windows(self):
    self.repo.cleanup_steps()
    sql = self.conn.joined_sql()
    self.assertIn("r.status = 'success'", sql)
    self.assertIn("INTERVAL '30 days'", sql)
    self.assertIn("r.status <> 'success'", sql)
    self.assertIn("INTERVAL '90 days'", sql)
```

另测：抢占返回零行不推；6 小时整不升级、超过 6 小时才升级；两个连接只有一个取得行锁；恢复发送成功才写 `state='resolved'`、`resolved_at=NOW()`；发送失败 rollback 后仍 open；resolved 后同类问题可再 claim；所有 SQL 参数化，payload 经过 `Jsonb`。

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_sync_alert_notifier.py" -v`

Expected: FAIL，repository 接口尚不存在。

- [ ] **Step 3: 实现 repository**

`claim_alert` 必须逐字使用：

```sql
INSERT INTO sync_job_alerts (job_id, run_id, alert_kind, payload_json)
VALUES (%s, %s, %s, %s)
ON CONFLICT (job_id, alert_kind) WHERE state = 'open' DO NOTHING
RETURNING id
```

`deliver_due` 在同一事务中 `SELECT ... FOR UPDATE SKIP LOCKED`，确认 `last_notified_at IS NULL OR last_notified_at < now - escalation_seconds` 后调用 sender；cutoff 必须参数化，默认值为 21600，不能把运行时 env 读成无效配置。只有 sender 返回 true 才更新 `last_notified_at`/`notify_count` 并 commit，否则 rollback。`resolve_alert` 同样先锁 open 行，发送恢复成功后再改 resolved。`load_job_states` 只读 `sync_jobs`、latest run、latest success、连续失败数与 open alerts，不 join/expose `external_sources.external_doc_id`。

清理 SQL：

```sql
DELETE FROM sync_job_steps s USING sync_job_runs r
WHERE s.run_id = r.id AND (
  (r.status = 'success' AND r.finished_at < NOW() - INTERVAL '30 days') OR
  (r.status <> 'success' AND r.finished_at < NOW() - INTERVAL '90 days')
)
```

- [ ] **Step 4: 验证状态机**

Run: `python -m unittest discover -s tests -p "test_sync_alert_notifier.py" -v`

Expected: 全绿。

- [ ] **Step 5: 提交**

```powershell
git add -- services/doc-sync-worker/app/pipelines/sync_alert_notifier.py tests/test_sync_alert_notifier.py
git commit -m "feat(sync): persist notifier alert state"
```

---

### Task 3: 六类巡检编排与飞书文本发送

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_alert_notifier.py`
- Modify: `tests/test_sync_alert_notifier.py`

**Interfaces:**
- Produces: `send_feishu_text(chat_id: str, text: str) -> bool`
- Produces: `run_notifier_once(*, repository=None, sender=None, now=None) -> dict[str, int]`
- Consumes: Task 1 判定/消息接口与 Task 2 repository。

- [ ] **Step 1: 写失败的完整状态机测试**

用 fake repository/sender 做确定性时钟，覆盖完整链：

```python
def test_failed_open_escalate_recover_and_reopen(self):
    first = self.run_with_latest(status="failed", run_id=10, error_kind="network")
    self.assertEqual({"opened": 1, "notified": 1}, pick(first, "opened", "notified"))
    self.advance(hours=6)
    self.assertEqual(0, self.run_with_latest(status="failed", run_id=10)["escalated"])
    self.advance(seconds=1)
    self.assertEqual(1, self.run_with_latest(status="failed", run_id=10)["escalated"])
    self.assertEqual(1, self.run_with_latest(status="success", run_id=11)["resolved"])
    self.assertEqual(1, self.run_with_latest(status="partial", run_id=12)["opened"])
```

同文件必须分别覆盖六件事：

1. latest failed/partial -> `failed` open；
2. open 超 6h -> 汇总升级；
3. 有 SLA 且 latest success 超时 -> `stale` open，恢复到 SLA 内 resolve；
4. 有 artifact_glob 且 snapshot/live glob 落后 -> `artifact_stale` open，产出物更新后 resolve；
5. token 低于 4 天 -> `credential_expiring` open，刷新后 resolve；
6. 新 success -> 对应 failed 告警恢复，并允许以后再开。

另测：`alert_enabled=false` 完全跳过；job `alert_chat_id` 覆盖 `SYNC_ALERT_CHAT_ID`；无群/无凭据只记录 open、发送失败后下轮可重试；同 job 可同时存在 failed/stale；父件核对业务消息函数未被调用；一次巡检末尾调用 cleanup。

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_sync_alert_notifier.py" -v`

Expected: FAIL，编排与 sender 尚不存在。

- [ ] **Step 3: 实现六件事与 sender**

`run_notifier_once` 顺序固定为：读取状态 -> 先 resolve 已恢复的 open（避免同轮先升级再恢复）-> 为仍异常的条件首次 claim -> 投递所有 due open（含刚 claim 和升级）-> cleanup。它从 `SYNC_ALERT_ESCALATION_SECONDS` 读取正整数并传给 repository，从 `SYNC_ARTIFACT_GRACE_SECONDS` 读取正整数并传给 artifact 判定；非法/非正值分别回落 21600/300。失败/partial payload 写 `error_kind`、脱敏 error message、last success、连续失败；stale/artifact/token payload 不写绝对路径或 token。

飞书 sender 复用 doc worker 已有凭据加载：

```python
profile = os.getenv("SYNC_ALERT_FEISHU_PROFILE", "COMPANY_A").strip() or "COMPANY_A"
creds = credentials_for_profile(profile)
client._request_json(
    "POST", "/im/v1/messages", headers=client._headers(),
    params={"receive_id_type": "chat_id"},
    json={"receive_id": chat_id, "msg_type": "text",
          "content": json.dumps({"text": text}, ensure_ascii=False)},
)
```

捕获发送异常时只打印异常类型，不打印响应体、凭据或 chat id。

- [ ] **Step 4: 验证六件事**

Run: `python -m unittest discover -s tests -p "test_sync_alert_notifier.py" -v`

Expected: 全绿，开→升级→恢复→再开完整链通过。

- [ ] **Step 5: 提交**

```powershell
git add -- services/doc-sync-worker/app/pipelines/sync_alert_notifier.py tests/test_sync_alert_notifier.py
git commit -m "feat(sync): run unified alert notifier"
```

---

### Task 4: T+ 产出物元数据与 `chanjet.full` 监控默认值

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_all.py`
- Modify: `services/tplus-sync-worker/tests/test_job_sync_all_platform.py`
- Modify: `services/doc-sync-worker/app/pipelines/sync_alert_notifier.py`
- Modify: `tests/test_sync_alert_notifier.py`

**Interfaces:**
- Produces: `sync_job_runs.detail_json.artifacts[] = {name, mtime, mtime_epoch}` for `chanjet.full`.
- Produces: `SyncAlertRepository.ensure_chanjet_defaults()` using COALESCE.
- Consumes: notifier artifact判定和 `/app/tplus-output/excel/*.xlsx` 只读 glob。

- [ ] **Step 1: 写失败的 artifact 与默认值测试**

```python
def test_platform_detail_records_artifact_name_and_mtime_without_path(self):
    result, writer = run_with_real_temp_exports()
    detail = writer.finish_calls[-1]["detail_json"]
    self.assertEqual("物料清单合并_20260812.xlsx", detail["artifacts"][0]["name"])
    self.assertIn("mtime_epoch", detail["artifacts"][0])
    self.assertNotIn(str(self.temp_dir), repr(detail))

def test_chanjet_defaults_only_fill_null_operator_fields(self):
    self.repo.ensure_chanjet_defaults()
    sql = self.conn.joined_sql()
    self.assertIn("freshness_sla_seconds = COALESCE(freshness_sla_seconds, %s)", sql)
    self.assertIn("artifact_glob = COALESCE(artifact_glob, %s)", sql)
    self.assertEqual((172800, "/app/tplus-output/excel/*.xlsx", "chanjet.full"), self.conn.params[-1])
```

覆盖所有实际成功 export 均通过统一 helper 记录；export 之后的失败仍保留已确认的 artifact；stat 失败只跳过该 artifact，不使同步失败；配置/顶层异常保持 artifacts 空数组；旧 `export_files`、exit code、steps、legacy attach 契约不变。

- [ ] **Step 2: 跑测试确认 RED**

Run from repo root: `python -m unittest discover -s tests -p "test_sync_alert_notifier.py" -v`

Run from `services/tplus-sync-worker` with `PYTHONPATH=src`: `python -m unittest discover -s tests -p "test_job_sync_all_platform.py" -v`

Expected: FAIL，artifact 结构/default updater 尚不存在。

- [ ] **Step 3: 记录 basename + mtime 并补 NULL 默认值**

在 T+ job 中用一个 `record_export(path)` helper 同时追加既有 basename 和 artifact 元数据；绝不保存绝对路径。`platform_detail()` 增加 `"artifacts": list(artifacts)`。

在 repository 中加入：

```sql
UPDATE sync_jobs
SET freshness_sla_seconds = COALESCE(freshness_sla_seconds, %s),
    artifact_glob = COALESCE(artifact_glob, %s),
    updated_at = NOW()
WHERE job_key = %s
```

notifier 每轮在读取作业前调用一次；只影响已经由 P1 登记的 `chanjet.full`。

- [ ] **Step 4: 验证 T+ 与 notifier 聚焦测试**

Run from repo root: `python -m unittest discover -s tests -p "test_sync_alert_notifier.py" -v`

Run from `services/tplus-sync-worker` with `PYTHONPATH=src`: `python -m unittest discover -s tests -p "test_job_sync_all*.py" -v`

Expected: 全绿。

- [ ] **Step 5: 提交**

```powershell
git add -- services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_all.py services/tplus-sync-worker/tests/test_job_sync_all_platform.py services/doc-sync-worker/app/pipelines/sync_alert_notifier.py tests/test_sync_alert_notifier.py
git commit -m "feat(sync): track T+ output freshness"
```

---

### Task 5: 接入 30 秒 worker poll 与生产只读挂载

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/worker_loop.py`
- Modify: `tests/test_doc_sync_worker.py`
- Modify: `local/docker-compose.local.yml`
- Modify: `deploy/ecs/compose.prod.yml`
- Modify: `deploy/ecs/deploy.sh`
- Modify: `deploy/ecs/release-meta.env.example`
- Modify: `docs/env-matrix.md`
- Create: `tests/test_compose_env.py`

**Interfaces:**
- Consumes: `run_notifier_once()` from Task 3.
- Produces: production `doc-sync-worker` receives `SYNC_ALERT_*`, read-only token file and read-only T+ output.
- Produces: `run_worker_loop(..., notifier_once=None)` test seam; injected/non-default pipelines do not touch DB/network unless notifier is explicitly injected.

- [ ] **Step 1: 写失败的 loop 与 compose 契约测试**

```python
def test_default_loop_runs_notifier_on_each_poll_without_changing_sync_result(self):
    events = []
    code = run_worker_loop(
        full_sync=lambda: events.append("full") or 0,
        consume_requests=lambda: events.append("pending") or 0,
        notifier_once=lambda: events.append("notifier") or {},
        schedule_reader=future_schedule, sleep=lambda _: None, max_cycles=1,
    )
    self.assertEqual(0, code)
    self.assertGreaterEqual(events.count("notifier"), 1)

def test_notifier_failure_is_fail_open(self):
    code = run_worker_loop(..., notifier_once=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    self.assertEqual(0, code)
```

Compose 文本测试必须断言 doc worker 有：

```yaml
SYNC_ALERT_CHAT_ID: ${SYNC_ALERT_CHAT_ID:-}
SYNC_ALERT_FEISHU_PROFILE: ${SYNC_ALERT_FEISHU_PROFILE:-COMPANY_A}
SYNC_ALERT_ESCALATION_SECONDS: ${SYNC_ALERT_ESCALATION_SECONDS:-21600}
SYNC_ARTIFACT_GRACE_SECONDS: ${SYNC_ARTIFACT_GRACE_SECONDS:-300}
CHANJET_OPEN_TOKEN_FILE: /app/tplus-sync-requests/chanjet_open_token.txt
```

以及 `tplus_sync_requests:/app/tplus-sync-requests:ro`、`tplus_sync_output:/app/tplus-output:ro`。测试 `deploy.sh` 把四个 `SYNC_*` 变量写入 `current.env`；example 只含空/占位值。

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v`

Run: `python -m unittest discover -s tests -p "test_compose_env.py" -v`

Expected: FAIL，loop/compose 尚未接入。

- [ ] **Step 3: 接入 fail-open notifier 与部署变量**

生产默认装配用 `run_notifier_once`；每个 30 秒 poll 在消费请求后调用一次，并在长周期开始前调用一次。调用异常只输出异常类型，不拖垮 full/pending/config pull。测试注入的 `full_sync`/`consume_requests` 路径若未显式传 notifier，保持 no-op。

在 local/prod compose 加只读挂载和变量；`deploy.sh` 从 server-private `release-meta.env` 读取并写入生成的 `current.env`。`release-meta.env.example` 使用：

```dotenv
SYNC_ALERT_CHAT_ID=
SYNC_ALERT_FEISHU_PROFILE=COMPANY_A
SYNC_ALERT_ESCALATION_SECONDS=21600
SYNC_ARTIFACT_GRACE_SECONDS=300
```

- [ ] **Step 4: 验证 loop 与两套 Compose**

Run: `python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v`

Run: `python -m unittest discover -s tests -p "test_compose_env.py" -v`

Run: `docker compose -f local/docker-compose.local.yml config`

Run: `docker compose -f deploy/ecs/compose.business-cn.yml config`

Expected: 全绿/exit 0；渲染输出的 doc worker 两个共享卷均为 read_only。

- [ ] **Step 5: 提交**

```powershell
git add -- services/doc-sync-worker/app/pipelines/worker_loop.py tests/test_doc_sync_worker.py local/docker-compose.local.yml deploy/ecs/compose.prod.yml deploy/ecs/deploy.sh deploy/ecs/release-meta.env.example docs/env-matrix.md tests/test_compose_env.py
git commit -m "feat(sync): run notifier in doc worker"
```

---

### Task 6: 下线 backend 两条旧告警线程

**Files:**
- Modify: `services/backend-api/app/routers/ops.py`
- Delete: `tests/test_backend_chanjet_token_alert.py`
- Delete: `tests/test_backend_tplus_sync_alert.py`
- Create: `tests/test_backend_sync_alert_retirement.py`

**Interfaces:**
- Consumes: Tasks 1–5 已验证的新 notifier、token 判据、48h SLA、群发送与 worker 接入。
- Produces: backend startup 不再创建 `chanjet-token-watcher` / `tplus-full-sync-watcher`；只读 `/v1/sync/*` 保持。

- [ ] **Step 1: 写旧线程必须消失的失败测试**

```python
def test_backend_no_longer_owns_sync_alert_threads(self):
    source = OPS.read_text(encoding="utf-8")
    for forbidden in (
        "_chanjet_token_alert_loop", "chanjet-token-watcher",
        "_tplus_full_sync_alert_loop", "tplus-full-sync-watcher",
        "CHANJET_ALERT_FEISHU_RECEIVE_ID",
    ):
        self.assertNotIn(forbidden, source)

def test_unified_notifier_is_wired_before_legacy_tests_are_retired(self):
    notifier = NOTIFIER.read_text(encoding="utf-8")
    loop = WORKER_LOOP.read_text(encoding="utf-8")
    self.assertIn("credential_expiring", notifier)
    self.assertIn("run_notifier_once", loop)
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_backend_sync_alert_retirement.py" -v`

Expected: FAIL，旧函数/线程仍存在。

- [ ] **Step 3: 删除旧实现与旧专属测试**

删除 `ops.py` 从“T+ openToken 有效期监控”到“T+ 定时全量同步结果监控”结束的两段及只为它们服务的 import/常量；删除两份旧测试。不要修改 timeline、sync-config、full-sync 或其他 ops 路由。确认公开仓中的硬编码 chat id 随旧常量一起消失。

- [ ] **Step 4: 验证接管后下线**

Run: `python -m unittest discover -s tests -p "test_backend_sync_alert_retirement.py" -v`

Run: `python -m unittest discover -s tests -p "test_backend_ops*.py" -v`

Run: `python -m unittest discover -s tests -p "test_backend_sync*.py" -v`

Expected: 全绿，backend 不再有告警线程且 P2 GET API 不变。

- [ ] **Step 5: 提交**

```powershell
git add -- services/backend-api/app/routers/ops.py tests/test_backend_sync_alert_retirement.py
git add -u -- tests/test_backend_chanjet_token_alert.py tests/test_backend_tplus_sync_alert.py
git commit -m "refactor(sync): retire legacy alert loops"
```

---

### Task 7: PostgreSQL 16 并发集成、导航与阶段交接

**Files:**
- Create: `tests/test_sync_alert_notifier_integration.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/project-ai-map.md`
- Modify: `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md`

**Interfaces:**
- Consumes: 完整 P3 notifier、0048 表结构与 CI PostgreSQL service。
- Produces: opt-in `SYNC_ALERT_INTEGRATION_DATABASE_URL` 真实库测试；P4 接手须知。

- [ ] **Step 1: 写真实 PostgreSQL 集成测试**

使用唯一 `ci.p3.<uuid>` job key，真实执行 repository SQL，sender 只记录文本、不调飞书。测试必须验证：

```python
first, second = concurrent_claim_two_connections(job_id, "failed")
self.assertEqual([False, True], sorted([bool(first), bool(second)]))
self.assertEqual(1, open_alert_count(job_id, "failed"))

self.assertTrue(repo.deliver_due(alert_id, fake_sender))
self.assertEqual((1, "open"), alert_notify_count_and_state(alert_id))
self.assertTrue(repo.resolve_alert(alert_id, recovery_payload, fake_sender))
self.assertEqual("resolved", alert_state(alert_id))
self.assertIsNotNone(repo.claim_alert(job, newer_run_id, "failed", {}))
```

再造 31/91 天 steps，验证 30/90 天清理边界；验证 `chanjet.full` 已有非 NULL operator 值不被默认值覆盖。`finally` 按精确 job id cascade 清理并断言 job/run/step/alert 残留 `0|0|0|0`；不得 mock DB，不得使用不合法的 `ci.*` 固定生产 job allowlist 路径写 worker。

- [ ] **Step 2: 本地默认 SKIP 与真实 PG RED/GREEN**

Run without env: `python -m unittest discover -s tests -p "test_sync_alert_notifier_integration.py" -v`

Expected: 1 skipped。

用临时 PostgreSQL 16 应用全部迁移后运行：

```powershell
$env:SYNC_ALERT_INTEGRATION_DATABASE_URL='postgresql://app:app@127.0.0.1:<mapped-port>/app'
python -m unittest discover -s tests -p "test_sync_alert_notifier_integration.py" -v
```

Expected: 先因接口未全接通而 RED；实现修正后 1 test OK，清理残留全零。临时容器按精确名称删除，并恢复 Docker Desktop 原启动状态。

- [ ] **Step 3: 加 CI、导航与 P4 交接**

CI 在 migrations 后、backend smoke 前加入：

```yaml
- name: Run sync alert notifier integration test
  env:
    SYNC_ALERT_INTEGRATION_DATABASE_URL: postgresql://app:app@localhost:5432/app
  run: python -m unittest discover -s tests -p "test_sync_alert_notifier_integration.py" -v
```

`docs/project-ai-map.md` 增加 notifier 文件和 `sync_job_alerts` 语义。spec 追加 P3 实施摘要与 `P4 接手须知`：P3 的 alert 表/loop 是生产事实源；P4 只加 scheduler shadow，不改变 notifier；P4 必须独立 PR/独立部署，不得与 P3 同批上线；本轮只到 shadow，不切 active。

- [ ] **Step 4: 全量验证**

Run: `python -m unittest discover -s tests -p "test_*.py"`

Run from `services/tplus-sync-worker` with `PYTHONPATH=src`: `python -m unittest discover -s tests -p "test_*.py"`

Run: `python scripts/check_navigation.py --root .`

Run: `docker compose -f local/docker-compose.local.yml config`

Run: `docker compose -f deploy/ecs/compose.business-cn.yml config`

Run: `git diff --check origin/main...HEAD`

Expected: 全部 exit 0；真实 PG 测试通过并清零；源码秘密扫描无 chat id/docid/token/secret 值。

- [ ] **Step 5: 提交**

```powershell
git add -- tests/test_sync_alert_notifier_integration.py .github/workflows/ci.yml docs/project-ai-map.md docs/superpowers/specs/2026-08-11-unified-sync-center-design.md
git commit -m "test(sync): verify P3 notifier against PostgreSQL"
```

---

## PR、部署与生产验收

完成全部任务和独立终审后：

1. 串行检查 `git status`、分支、remote、提交范围与敏感路径；push `codex/unified-sync-center-p3`，创建 ready PR。
2. 等 `migration-dry-run`、`validate`、`update-pr-body` 全绿后 squash merge；不绕过 PR CI。
3. 在 txecs 的 server-private secret 来源中设置 `SYNC_ALERT_CHAT_ID`，不得把值写入仓库；按 `infra/secrets/README.md` 的 SOPS 流程持久化，渲染到 `release-meta.env`。
4. 手工触发 `release-deploy.yml -f deploy_target=business-cn`；P3 与 P4 不同批。
5. 验收 `stage-business-cn-peer=success`（`deploy-business-cn=skipped` 正常），并通过 SSH 验证 backend/doc-sync-worker 镜像、启动时间、health/log。
6. 生产只读核验：旧 backend 两 watcher 不存在；doc worker 每轮 notifier 无异常；`chanjet.full` SLA=172800 且 artifact_glob 已补；正常状态不伪造 open alert。
7. 如生产恰有真实失败/token 预警，核验 `sync_job_alerts` 与飞书文本一致；如全健康，不注入生产假故障，仅把人工检查项交给用户。
8. 单独提交/合并 P3 生产证据文档（文档 PR 不再次部署），随后从最新 main 写 P4 计划。
