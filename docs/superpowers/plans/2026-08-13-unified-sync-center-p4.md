# Unified Sync Center P4 Scheduler Shadow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收敛 doc-sync 与 T+ 的复制调度算法、把配置面双写到 `sync_jobs.schedule`，并以独立 P4 发布让两个 worker 进入可自动对账的 `shadow` 模式；本轮绝不切 `active`。

**Architecture:** 两个 worker 各自随镜像携带一份字节完全一致的纯 Python `sync_scheduler.py`，一致性测试防止复制漂移。legacy 逻辑继续决定真实执行；shadow 内核读取 `sync_jobs.schedule` 并把新旧决策、due 差、计划/实际睡眠写进既有 scheduled run 的 `detail_json.shadow`，不创建新事实表、不创建会影响 freshness 的伪运行。`active` 代码路径只做单测和未来切换准备，生产 profile 固定为两个 worker 都 `shadow`。

**Tech Stack:** Python 3.12、`unittest`、PostgreSQL 16/JSONB、psycopg 3、Docker Compose、GitHub Actions、SOPS + age。

## Global Constraints

- 本计划只完成 P4 shadow 上线并开始采样；不得把 doc-sync 或 T+ 任一 worker 切为 `active`，不得触碰 P5。
- P4 必须是 P3 之后的独立 branch、PR、merge 和 `business-cn` 部署；不得复用 P3 发布证据。
- 用户已明确授权不等待完整 14 天 baseline；用现有长期 legacy 记录、全量人工核对和可恢复能力替代日历等待，但仍保留 shadow 自动证据与 `legacy` 回滚门。
- 生产实读基线（2026-08-13）：`doc_sync` 为 enabled、86400 秒、北京时间 `15:30`；`chanjet` 为 enabled、86400 秒、北京时间 `01:00`。两边均有锚点，因此 T+ 无锚点差异当前不触发。
- 容器内开关名必须是 `SYNC_SCHEDULER_MODE=legacy|shadow|active`；宿主 profile 用 `DOC_SYNC_SCHEDULER_MODE` / `TPLUS_SYNC_SCHEDULER_MODE` 分开注入，保证未来可以逐 worker 切换。
- 未知、空白或大小写异常 mode 必须 fail closed 到 `legacy`；本轮 SOPS 两个 mode 都必须精确为 `shadow`。
- shadow 不得改变 run_full、due、wait、热唤醒或手动/事件请求的真实执行；只允许 fail-open 元数据写入和日志。
- `sync_jobs.schedule` 是候选配置面；legacy `integration_sync_config` 在 P4 继续保留并原子双写，供 shadow 对照与 `legacy` 回滚。
- doc-sync 的飞书配置拉取能力保持仅 doc-sync 生效；不得扩展到 T+。
- shadow 证据只写已有真实 scheduled run 的 `detail_json.shadow`；不得创建伪成功 run、不得刷新 notifier 的成功时间、不得新增表或迁移。
- PostgreSQL 更新必须参数化、事务失败 rollback、writer 全链路 fail-open；`sync_jobs.updated_at` 每次显式 `NOW()`。
- 公开仓库不得出现真实 chat id、docid、token、数据库密码或生产路径泄露；fixture 用 `ci.p4.<uuid>` 并精确清理。
- 测试命令统一使用 `python -m unittest discover -s tests -p "test_xxx.py"`；T+ 在子目录以 `PYTHONPATH=src` 运行。
- Git 走 `codex/unified-sync-center-p4` + PR；每任务独立 commit，只 add 明确文件，不用 `git add -A`。

## File Structure

- `services/doc-sync-worker/app/pipelines/sync_scheduler.py`：共享候选内核的 doc 镜像副本；只依赖标准库。
- `services/tplus-sync-worker/src/tplus_datahub/jobs/sync_scheduler.py`：与上一文件字节完全相同的 T+ 镜像副本。
- `services/doc-sync-worker/app/pipelines/sync_schedule.py`、`app/storage/postgres.py`：doc legacy/platform 配置读写和 shadow JSONB 持久化。
- `services/tplus-sync-worker/src/tplus_datahub/jobs/db_sync_requests.py`：T+ platform schedule 与 shadow 持久化。
- `services/backend-api/app/routers/ops.py`：现有两个配置 PUT 在同一事务内双写 legacy + platform schedule。
- 两个 `worker_loop.py`：legacy/shadow/active 选择、计划/实际睡眠采样；shadow 不改变控制流。
- `tests/test_sync_scheduler.py`：共享内核、字节一致性、旧实现等价性、mode 选择。
- `tests/test_sync_scheduler_storage.py`：backend/doc repository SQL 与 fail-open 单测。
- `tests/test_sync_scheduler_integration.py`：opt-in PostgreSQL 16 JSONB/事务/清理集成。
- `tests/test_doc_sync_worker.py`、T+ `tests/test_worker_loop.py`：两个 loop 的行为/热唤醒/rollback 回归。
- Compose、deploy env、`tests/test_compose_env.py`：两个宿主 mode 独立注入同名容器 env。

---

### Task 1: Shared scheduler kernel and parity contract

**Files:**
- Create: `services/doc-sync-worker/app/pipelines/sync_scheduler.py`
- Create: `services/tplus-sync-worker/src/tplus_datahub/jobs/sync_scheduler.py`
- Create: `tests/test_sync_scheduler.py`

**Interfaces:**
- Produces: `ScheduleDecision(due, run_full, wait_seconds)`；`normalize_mode(raw) -> str`；`decide(now, last_full, enabled, interval_seconds, anchor_time) -> ScheduleDecision`；`target_moved_earlier(...) -> bool`；`shadow_payload(...) -> dict[str, object]`。
- `due` 一律 aware UTC；naive `last_full` 明确按 UTC 归一；interval 最小 60 秒；锚点按北京时间。

- [ ] **Step 1: Write failing shared/parity tests**

```python
def test_worker_scheduler_copies_are_byte_identical(self):
    self.assertEqual(DOC_SCHEDULER.read_bytes(), TPLUS_SCHEDULER.read_bytes())

def test_no_anchor_uses_doc_semantics(self):
    decision = scheduler.decide(NOW, LAST_FULL, True, 86400, "")
    self.assertFalse(decision.run_full)
    self.assertEqual(LAST_FULL + timedelta(days=1), decision.due)

def test_shadow_payload_is_json_safe_and_compares_literal_values(self):
    payload = scheduler.shadow_payload(sampled_at=NOW, legacy=LEGACY, candidate=CANDIDATE)
    self.assertEqual({"decision_match": True, "due_delta_seconds": 0.0}, {
        key: payload[key] for key in ("decision_match", "due_delta_seconds")
    })
```

表驱动 literal cases 必含：首次运行、跨日、36 小时间隔、锚点在 last_full 之前/之后、`last_full == anchor`、naive last_full、disabled、due 恰等 now、无锚点。另导入两份旧函数，确认当前生产有锚点输入下三者 due/run_full 等价；无锚点显式记录候选采用 doc 语义。

- [ ] **Step 2: Run RED**

Run: `python -m unittest discover -s tests -p "test_sync_scheduler.py" -v`

Expected: FAIL because both modules/interfaces do not exist.

- [ ] **Step 3: Implement the pure kernel once, then copy it byte-for-byte**

```python
@dataclass(frozen=True)
class ScheduleDecision:
    due: datetime
    run_full: bool
    wait_seconds: int

def normalize_mode(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    return value if value in {"legacy", "shadow", "active"} else "legacy"

def decide(now, last_full, enabled, interval_seconds, anchor_time):
    due = next_due(now, last_full, interval_seconds, anchor_time)
    run_full = bool(enabled and due <= now)
    return ScheduleDecision(due, run_full, max(int((due - now).total_seconds()), 0))
```

`target_moved_earlier` 使用 30 秒容差；`shadow_payload` 只返回 ISO8601、bool、int/float/null，不返回 datetime 对象或异常文本。

- [ ] **Step 4: Run GREEN and mutation probes**

Run: `python -m unittest discover -s tests -p "test_sync_scheduler.py" -v`

手动 mutation 检查：把其中一份文件追加空格，一致性测试必须失败；把 `<=` 改 `<`，恰等 due 用例必须失败。随后还原并重跑 GREEN。

- [ ] **Step 5: Commit exact files**

```powershell
git add -- services/doc-sync-worker/app/pipelines/sync_scheduler.py services/tplus-sync-worker/src/tplus_datahub/jobs/sync_scheduler.py tests/test_sync_scheduler.py
git commit -m "feat(sync): add shared scheduler kernel"
```

### Task 2: Unified platform schedule surface and fail-open shadow storage

**Files:**
- Modify: `services/backend-api/app/routers/ops.py`
- Modify: `services/doc-sync-worker/app/storage/postgres.py`
- Modify: `services/doc-sync-worker/app/pipelines/sync_schedule.py`
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/db_sync_requests.py`
- Create: `tests/test_sync_scheduler_storage.py`
- Modify: existing backend/doc/T+ storage tests selected by the implementer only when their current contract changes.

**Interfaces:**
- Produces doc `read_platform_schedule() -> dict | None` and store methods `seed_platform_schedule(schedule)`, `record_scheduler_shadow(payload) -> list[int]`, `finish_scheduler_shadow(run_ids, observed_sleep_seconds, candidate_would_wake)`.
- Produces T+ `fetch_platform_schedule(job_key="chanjet.full")`, `seed_platform_schedule(...)`, `record_scheduler_shadow(...)`, `finish_scheduler_shadow(...)` with matching payload semantics.
- Backend PUTs atomically write `integration_sync_config` and `sync_jobs.schedule`; doc Feishu pull uses the same transaction through `PostgresStore.upsert_sync_config`.

- [ ] **Step 1: Write SQL-contract RED tests**

```python
def test_tplus_put_dual_writes_schedule_in_one_transaction(self):
    response = client.put("/v1/ops/tplus/sync-config", json=CONFIG, headers=ADMIN)
    self.assertEqual(200, response.status_code)
    self.assertIn("UPDATE sync_jobs", fake_cursor.statements)
    self.assertEqual(1, fake_conn.commits)

def test_shadow_updates_latest_real_schedule_runs_only(self):
    run_ids = store.record_scheduler_shadow(PAYLOAD)
    self.assertEqual([101, 102], run_ids)
    self.assertIn("trigger = 'schedule'", normalized_sql)
    self.assertNotIn("INSERT INTO sync_job_runs", normalized_sql)
```

另测 doc selector 仅 `kind='pull' AND provider IN ('wecom','feishu')`，T+ 仅 `job_key='chanjet.full'`；空 platform schedule 只 seed 一次；非空 schedule 不被 worker legacy 值覆盖；任一 SQL 失败 rollback 且原 legacy 配置路径仍成功。

- [ ] **Step 2: Run RED**

Run: `python -m unittest discover -s tests -p "test_sync_scheduler_storage.py" -v`

Expected: FAIL on missing methods and missing dual-write SQL.

- [ ] **Step 3: Implement parameterized JSONB storage**

Core update shape:

```sql
UPDATE sync_jobs
SET schedule = %s, updated_at = NOW()
WHERE kind = 'pull' AND provider IN ('wecom', 'feishu');

WITH latest AS (
  SELECT DISTINCT ON (r.job_id) r.id
  FROM sync_job_runs r JOIN sync_jobs j ON j.id = r.job_id
  WHERE r.trigger = 'schedule'
    AND j.kind = 'pull'
    AND j.provider IN ('wecom', 'feishu')
  ORDER BY r.job_id, r.started_at DESC
)
UPDATE sync_job_runs r
SET detail_json = jsonb_set(r.detail_json, '{shadow}', %s, true)
FROM latest WHERE r.id = latest.id
RETURNING r.id;
```

`finish_scheduler_shadow` 只按返回的精确 run ids 更新 `observed_sleep_seconds` / `candidate_would_wake`，不重新按“latest”选择，防止睡眠期间的新 run 被串写。所有 payload 用 psycopg `Jsonb`，失败 rollback 后返回空/None。

- [ ] **Step 4: Run storage and adjacent tests**

```powershell
python -m unittest discover -s tests -p "test_sync_scheduler_storage.py" -v
python -m unittest discover -s tests -p "test_backend_tplus*.py" -v
python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v
Push-Location services/tplus-sync-worker; $env:PYTHONPATH='src'; python -m unittest discover -s tests -p "test_db_sync_requests.py" -v; Pop-Location
```

- [ ] **Step 5: Commit exact changed files**

```powershell
git add -- services/backend-api/app/routers/ops.py services/doc-sync-worker/app/storage/postgres.py services/doc-sync-worker/app/pipelines/sync_schedule.py services/tplus-sync-worker/src/tplus_datahub/jobs/db_sync_requests.py tests/test_sync_scheduler_storage.py tests/test_backend_ops_status.py tests/test_doc_sync_worker.py services/tplus-sync-worker/tests/test_db_sync_requests.py
git commit -m "feat(sync): mirror schedules into platform metadata"
```

### Task 3: Doc worker shadow/active wiring without shadow behavior change

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/worker_loop.py`
- Modify: `tests/test_doc_sync_worker.py`

**Interfaces:**
- Consumes Task 1 `decide/normalize_mode/target_moved_earlier/shadow_payload` and Task 2 doc storage interfaces.
- Produces `scheduler_mode_reader`, `platform_schedule_reader`, `shadow_recorder`, `shadow_finisher` dependency injection points for deterministic tests.

- [ ] **Step 1: Write loop RED tests**

Cover exact modes:

```python
def test_shadow_records_candidate_but_legacy_still_drives_run(self):
    run_forever(mode_reader=lambda: "shadow", legacy_schedule_reader=LEGACY_DUE,
                platform_schedule_reader=lambda: CANDIDATE_NOT_DUE, max_runs=1)
    self.assertEqual(1, full_sync.calls)       # legacy behavior unchanged
    self.assertFalse(shadow["decision_match"])

def test_active_uses_candidate_and_doc_hot_wakes_within_one_poll(self):
    # Candidate target moves earlier on the second 30s poll.
    self.assertEqual([30], sleeps)
    self.assertEqual(0, full_sync.calls)
```

另测 `legacy` 不读/不写 platform；`shadow` storage 异常 fail-open；shadow config 提前只记录 `candidate_would_wake=true` 但不提前结束 legacy sleep；unknown mode 等同 legacy；手动请求与 notifier poll 次数不变；observed sleep 写回精确 run ids。

- [ ] **Step 2: Run RED**

Run: `python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v`

Expected: only new scheduler tests fail.

- [ ] **Step 3: Add mode selection around, not inside, legacy behavior**

```python
mode = normalize_mode(mode_reader())
legacy_decision = existing_legacy_decision(...)
candidate = decide(current, last_full, **platform_config)
actual = candidate if mode == "active" else legacy_decision
if mode == "shadow":
    shadow_run_ids = shadow_recorder(shadow_payload(...))
```

Legacy branch must retain current notifier terminal-poll dedupe and disabled recheck. Shadow wake predicate is observed only; active predicate may break sleep. Always finalize observed duration in `finally`, but persistence failure must not alter loop result.

- [ ] **Step 4: Run GREEN plus adjacent suites**

```powershell
python -m unittest discover -s tests -p "test_doc_sync_worker.py" -v
python -m unittest discover -s tests -p "test_sync_alert_notifier.py" -v
```

- [ ] **Step 5: Commit exact files**

```powershell
git add -- services/doc-sync-worker/app/pipelines/worker_loop.py tests/test_doc_sync_worker.py
git commit -m "feat(sync): shadow doc scheduler decisions"
```

### Task 4: T+ worker shadow/active wiring and legacy no-anchor proof

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/worker_loop.py`
- Modify: `services/tplus-sync-worker/tests/test_worker_loop.py`

**Interfaces:**
- Consumes Tasks 1–2 shared kernel and T+ storage interfaces.
- Adds the same dependency injection points as doc worker; container mode remains external env `SYNC_SCHEDULER_MODE`.

- [ ] **Step 1: Write T+ RED tests**

```python
def test_shadow_with_no_anchor_keeps_legacy_restart_full_run(self):
    run_forever(mode_reader=lambda: "shadow", read_last_full=lambda: RECENT,
                read_sync_config=lambda: NO_ANCHOR, max_runs=1)
    self.assertEqual(1, sync_once.calls)
    self.assertFalse(recorded_shadow["decision_match"])

def test_shadow_with_production_anchor_matches_without_changing_execution(self):
    self.assertEqual({"decision_match": True, "due_delta_seconds": 0.0}, comparison)
```

另测 legacy 现有 `_schedule_target_moved_earlier` 行为不变；shadow writer failure fail-open；active uses candidate doc semantics；manual BOM/full requests continue during every sleep slice；observed sleep finalized on early wake and normal due.

- [ ] **Step 2: Run RED**

```powershell
Push-Location services/tplus-sync-worker
$env:PYTHONPATH='src'
python -m unittest discover -s tests -p "test_worker_loop.py" -v
Pop-Location
```

- [ ] **Step 3: Implement the same mode boundary as Task 3**

Do not delete `next_scheduled_full_due`, `_seconds_until_next_due`, or legacy wake code; `legacy` rollback must remain a local env flip. Candidate config reads `sync_jobs.schedule`, seeding only when empty.

- [ ] **Step 4: Run full T+ suite**

```powershell
Push-Location services/tplus-sync-worker
$env:PYTHONPATH='src'
python -m unittest discover -s tests -p "test_*.py"
Pop-Location
```

- [ ] **Step 5: Commit exact files**

```powershell
git add -- services/tplus-sync-worker/src/tplus_datahub/jobs/worker_loop.py services/tplus-sync-worker/tests/test_worker_loop.py
git commit -m "feat(sync): shadow T+ scheduler decisions"
```

### Task 5: Separate per-worker deployment switches, shadow defaults and rollback guards

**Files:**
- Modify: `local/docker-compose.local.yml`
- Modify: `deploy/ecs/compose.prod.yml`
- Modify: `deploy/ecs/deploy.sh`
- Modify: `deploy/ecs/release-meta.env.example`
- Modify: `docs/env-matrix.md`
- Modify: `tests/test_compose_env.py`
- Modify: `deploy/ecs/tests/test_deploy_roles.sh`

**Interfaces:**
- Host inputs: `DOC_SYNC_SCHEDULER_MODE`, `TPLUS_SYNC_SCHEDULER_MODE`.
- Container input for each worker: `SYNC_SCHEDULER_MODE`.
- Allowed values exactly `legacy|shadow|active`; deploy script rejects anything else before Compose.

- [ ] **Step 1: Write failing rendered-Compose and deploy-guard tests**

Assert local/prod rendered config gives doc `shadow` and T+ `shadow`; changing only doc host input to `active` must leave T+ `shadow`. Assert missing inputs default `legacy` in code/example safety tests, while checked-in deployment example explicitly selects `shadow`. Assert invalid `SHADOW`, blank or `active-now` is rejected by deploy guard.

- [ ] **Step 2: Run RED**

```powershell
python -m unittest discover -s tests -p "test_compose_env.py" -v
bash deploy/ecs/tests/test_deploy_roles.sh
```

- [ ] **Step 3: Add exact env wiring**

```yaml
# doc service
SYNC_SCHEDULER_MODE: ${DOC_SYNC_SCHEDULER_MODE:-legacy}
# tplus service
SYNC_SCHEDULER_MODE: ${TPLUS_SYNC_SCHEDULER_MODE:-legacy}
```

`deploy.sh` writes both host inputs into runtime env and validates with a shared shell case; do not introduce one global host switch that prevents逐 worker rollback.

- [ ] **Step 4: Validate scripts and both Compose variants**

```powershell
bash -n deploy/ecs/deploy.sh
bash deploy/ecs/tests/test_deploy_roles.sh
docker compose -f local/docker-compose.local.yml config > $null
docker compose --env-file deploy/ecs/runtime.env.example -f deploy/ecs/compose.business-cn.yml config > $null
python -m unittest discover -s tests -p "test_compose_env.py" -v
```

- [ ] **Step 5: Commit exact files**

```powershell
git add -- local/docker-compose.local.yml deploy/ecs/compose.prod.yml deploy/ecs/deploy.sh deploy/ecs/release-meta.env.example docs/env-matrix.md tests/test_compose_env.py deploy/ecs/tests/test_deploy_roles.sh
git commit -m "feat(sync): configure per-worker scheduler modes"
```

### Task 6: Real PostgreSQL shadow evidence integration and P4 handoff docs

**Files:**
- Create: `tests/test_sync_scheduler_integration.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/project-ai-map.md`
- Modify: `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md`

**Interfaces:**
- Opt-in env: `SYNC_SCHEDULER_INTEGRATION_DATABASE_URL`; missing env skips exactly one test.
- CI runs after all migrations, after P1/P2/P3 integrations, before backend smoke.

- [ ] **Step 1: Write opt-in PostgreSQL test**

The test must use real psycopg connections and real repository SQL, not mock cursors. It creates exact `ci.p4.<uuid>` jobs/runs plus a preserved `chanjet.full` fixture, proves:

```python
self.assertEqual(CONFIG, stored_schedule)
self.assertEqual("shadow", detail_json["shadow"]["mode"])
self.assertEqual([run_id], exact_updated_ids)
self.assertEqual(123, detail_json["shadow"]["observed_sleep_seconds"])
self.assertEqual(0, synthetic_shadow_run_count)
```

Also force a second write failure and prove rollback leaves connection queryable. `finally` restores pre-existing `chanjet.full.schedule/updated_at`, deletes exact CI jobs, and asserts job/run/step/alert residue `0|0|0|0`.

- [ ] **Step 2: Run default SKIP and real PostgreSQL GREEN**

Default:

```powershell
python -m unittest discover -s tests -p "test_sync_scheduler_integration.py" -v
```

Expected: one skipped. Then run against PostgreSQL 16 with all migrations applied using `psql -v ON_ERROR_STOP=1`; expected one passed and cleanup zeros. If local Docker is unavailable, PR `migration-dry-run` success is a mandatory merge gate and its log must show the test `... ok`, `Ran 1 test`, `OK` rather than skip.

- [ ] **Step 3: Wire CI and update navigation/handoff**

CI installs both worker requirements before executing the new test. Project map states: platform schedule is candidate config; legacy remains rollback; shadow evidence lives on real scheduled runs. Spec P4 handoff records the production config baseline and explicitly says this release begins shadow only.

- [ ] **Step 4: Full verification**

```powershell
python -m unittest discover -s tests -p "test_*.py"
Push-Location services/tplus-sync-worker; $env:PYTHONPATH='src'; python -m unittest discover -s tests -p "test_*.py"; Pop-Location
python scripts/check_navigation.py --root .
docker compose -f local/docker-compose.local.yml config > $null
docker compose --env-file deploy/ecs/runtime.env.example -f deploy/ecs/compose.business-cn.yml config > $null
git diff --check origin/main...HEAD
```

- [ ] **Step 5: Commit exact files**

```powershell
git add -- tests/test_sync_scheduler_integration.py .github/workflows/ci.yml docs/project-ai-map.md docs/superpowers/specs/2026-08-11-unified-sync-center-design.md
git commit -m "test(sync): verify scheduler shadow metadata"
```

### Task 7: Independent review, PR, SOPS shadow render and production start

**Files:**
- No product code changes unless final review/CI finds a concrete defect.
- Infra follow-up changes only: `infra/secrets/txecs.enc.env`, `infra/secrets/txecs-production.enc.env` (SOPS-encrypted).
- Evidence follow-up: `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md` in a docs-only PR after deployment.

**Interfaces:**
- Production desired state: both containers report `SYNC_SCHEDULER_MODE=shadow`; no container reports `active`.
- Rollback: set only the affected host input to `legacy`, render, and recreate that worker.

- [ ] **Step 1: Final whole-branch review**

Reviewer must inspect `origin/main...HEAD`, run focused parity/storage/loop tests, and classify Critical/Important/Minor. No open Critical/Important may proceed. Explicit probes: shadow cannot change actual run; synthetic runs are absent; notifier freshness unaffected; exact run-id finalize; unknown mode legacy; byte copies identical.

- [ ] **Step 2: Push PR and enforce CI gates**

PR body uses `Nav-Impact: updated`. Require `validate` and `migration-dry-run` success; inspect PostgreSQL log for the P4 test actual `ok`/`OK`. Merge squash only after gates and final review APPROVE.

- [ ] **Step 3: Persist two shadow modes in SOPS**

Without printing values:

```powershell
sops set infra/secrets/txecs.enc.env '["DOC_SYNC_SCHEDULER_MODE"]' '"shadow"'
sops set infra/secrets/txecs.enc.env '["TPLUS_SYNC_SCHEDULER_MODE"]' '"shadow"'
sops set infra/secrets/txecs-production.enc.env '["DOC_SYNC_SCHEDULER_MODE"]' '"shadow"'
sops set infra/secrets/txecs-production.enc.env '["TPLUS_SYNC_SCHEDULER_MODE"]' '"shadow"'
```

Commit/push only the two encrypted files, sync device remotes, fast-forward txecs infra worktree, render production profile, and assert the four keys equal `shadow` without displaying unrelated secrets.

- [ ] **Step 4: Deploy P4 separately**

```powershell
gh workflow run release-deploy.yml --ref main -f deploy_target=business-cn
```

Require `stage-business-cn-peer=success`; `deploy-business-cn=skipped` is normal. Verify txecs source SHA and both worker new start times/images.

- [ ] **Step 5: Prove shadow started and active did not**

After at least one loop preflight (not seven days), query production:

```sql
SELECT job_key, schedule FROM sync_jobs
WHERE job_key = 'chanjet.full' OR (kind='pull' AND provider IN ('wecom','feishu'));

SELECT j.job_key, r.id, r.detail_json->'shadow'
FROM sync_job_runs r JOIN sync_jobs j ON j.id=r.job_id
WHERE r.trigger='schedule' AND r.detail_json ? 'shadow'
ORDER BY r.started_at DESC;
```

Acceptance for this session: both containers expose mode key `shadow`; zero `active`; legacy actual schedule remains 01:00/15:30; at least one T+ and one doc run has JSON-safe shadow evidence; `decision_match=true`; `due_delta_seconds <= 30`; no new run caused by shadow; manual/event counts unchanged; worker logs have no scheduler traceback; existing notifier remains healthy. If next real cycle has not occurred, `observed_sleep_seconds` may be null and is explicitly deferred to ongoing shadow evidence, not fabricated.

- [ ] **Step 6: Record evidence in docs-only PR and stop**

Append PR/CI/deploy SHA, run ID, image/start times, config baseline, first shadow samples, active count zero, and rollback command to the design spec. Merge docs-only PR without another deploy. Do not start P5 and do not change either mode to active.

## Self-Review

- Spec §5.1–5.3: Tasks 1–4 cover shared kernel, no central scheduler, doc semantics and hot wake.
- Spec §5.4: production config/history is read before coding; user-approved shortened baseline is recorded without deleting metric requirements.
- Spec §5.5: Tasks 2–6 persist and test new/old decision, due and wait evidence; deployment only starts shadow.
- Spec §5.6: Task 5 creates per-worker switches; Task 7 proves rollback and explicitly stops before active.
- No migration, new service, new table, P5 redirect, notifier semantic change, secret literal or synthetic freshness run is planned.
- Placeholder scan clean；later interfaces match Task 1/2 names.
