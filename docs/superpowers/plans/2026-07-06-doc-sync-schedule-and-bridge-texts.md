# 文档同步调度（DB 权威+起点时间+飞书配置表拉取）+ bridge 文案表格化 实施计划（PR②③）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ①文档同步（企微+飞书）的开关/周期/起点时间由 DB 控制、页面可看可改、worker 每轮热读，并支持从飞书多维表格「配置表」单向拉取（表格=编辑面，DB=生效面）；顺带修掉"worker 重启即跑全量"。②bridge 的 4 项飞书文案（占位卡/追问/空回复/完成标记）纳入规则表 global-default 行，表格可改、TTL 缓存、env/默认值兜底。

**Architecture:**
- PR②（AliECS 主服务）：迁移 0025 给 `integration_sync_config` 加 `anchor_time`/`pull_paused` 列并 seed `provider='doc_sync'` 行；doc-sync-worker 新增 `app/pipelines/sync_schedule.py`（配置读取/相位对齐计算/配置表拉取校验）；`worker_loop` 改为调度驱动；backend `ops.py` 加 GET/PUT `/v1/ops/doc-sync/sync-config`；exports 页加设置卡。
- PR③（bridge 单文件）：`feishu_global_rule_policy` 缓存从 2 个 bool 扩到 +4 个文本键；`ensure_feishu_default_rule_record` 建列并回填默认文案；4 个文案 getter 优先取表格非空值。

**Tech Stack:** Python 3.11 / psycopg / FastAPI / unittest；bridge 为 `deploy/openclaw-bridge/openclaw_bridge.py` 单文件。

## Global Constraints

- 分支：PR② `feature/doc-sync-schedule-config`、PR③ `feature/bridge-texts-in-rule-table`，均从 `origin/main` 建，走 PR。
- 测试：`python -m pytest tests/test_doc_sync_worker.py tests/test_backend_ops_status.py tests/test_exports_frontend.py -q`（PR②）、`python -m pytest tests/test_openclaw_bridge.py -q`（PR③）；提交前全量 `python -m pytest tests/ -q`。
- 北京时间=UTC+8 固定偏移（无夏令时），起点时间格式 `HH:MM`，空串=不锚定（沿用"上次+interval"）。
- 既有两个 WorkerLoopTests 必须保持通过（注入 full_sync/consume_requests/sleep/max_cycles 的调用方式不变；DB 不可用时回退 env 行为）。
- 配置表约定：飞书多维表格中名为「配置表」的数据表，四列：配置键/配置值/说明/状态；识别键：`文档同步开关`(truthy)、`文档同步周期小时`(1-168 float)、`文档同步起点时间`(HH:MM或空)；状态列非空且≠"启用"的行跳过；未知键忽略；非法值跳过并打日志。
- 冲突规则：管理页 PUT=应急覆盖并可置 `pull_paused=true`；puller 在 `pull_paused` 时不写库。
- bridge 不动 OpenClaw 本体；bridge 上线走手动 cutover（V-tag + ECS docker rm -f + compose up）。
- git add 显式列文件。

---

## PR② 任务

### Task 1: 迁移 0025 + store 三方法

**Files:** Create `db/migrations/0025_doc_sync_schedule.sql`；Modify `services/doc-sync-worker/app/storage/postgres.py`；Test `tests/test_doc_sync_worker.py`

**Produces:**
- SQL：`ALTER TABLE integration_sync_config ADD COLUMN IF NOT EXISTS anchor_time text NOT NULL DEFAULT ''; ADD COLUMN IF NOT EXISTS pull_paused boolean NOT NULL DEFAULT false; INSERT provider='doc_sync' ON CONFLICT DO NOTHING;`
- `PostgresDocSyncStore.get_sync_config(provider) -> dict | None`（enabled/interval_seconds/anchor_time/pull_paused/updated_at/updated_by）
- `PostgresDocSyncStore.upsert_sync_config(provider, enabled, interval_seconds, anchor_time, updated_by) -> None`（不改 pull_paused）
- `PostgresDocSyncStore.last_full_run_started_at() -> datetime | None`（`SELECT MAX(started_at) FROM sync_runs WHERE mode='full' AND provider IN ('wecom','feishu')`）

store 方法纯 SQL 无独立单测（沿用仓库惯例，逻辑测试全走 Fake store）。

### Task 2: `sync_schedule.py` 纯逻辑（TDD 重点）

**Files:** Create `services/doc-sync-worker/app/pipelines/sync_schedule.py`；Test `tests/test_doc_sync_worker.py` 新增 `SyncScheduleTests`

**Produces:**
- `parse_config_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]`：入参为 normalize 后的行 dict（键=列名），返回 (`{"enabled":bool,"interval_seconds":int,"anchor_time":str}` 的子集, 错误列表)。
- `next_full_sync_due(now: datetime, last_full: datetime | None, interval_seconds: int, anchor_time: str) -> datetime`：均为 aware-UTC。规则：last_full 为 None → 返回 now（立即）；anchor 空 → last_full+interval；anchor `HH:MM`（北京时间）→ 取序列 {北京当日 anchor + k*interval, k∈ℤ} 中大于 last_full 的最小值。
- `read_schedule_config() -> dict`：开 store 读 `doc_sync` 行；任何异常回退 `{"enabled": True, "interval_seconds": env DOC_SYNC_INTERVAL_SECONDS 或 86400, "anchor_time": "", "pull_paused": False}`。
- `pull_config_from_bitable() -> str`：状态字符串（记日志用）。开 store → 找 `provider='feishu'` `sheet_name='配置表'` active bitable_table 源（用新 store 查询或 list_bitable_sources 过滤）→ 该 profile 凭据建 client → list_fields+get_records → normalize_record → parse_config_rows → `pull_paused` 或无变化则不写 → 否则 upsert_sync_config(updated_by='feishu-config-table')。任何异常返回错误串不抛。

测试用例（全部 Fake，不碰网络/DB）：
1. parse：三键合法（"true"/"√"勾选值、"6"、"02:00"）→ 正确转换；周期 0.5/200、起点 "25:00" → 进错误列表且不出现在结果。
2. next_due：无锚点=last+interval；锚点 02:00 北京+24h 周期，last=UTC 前日 18:05（北京 02:05）→ due=UTC 当日 18:00；last=None→now。
3. 锚点+6h 周期相位：02/08/14/20 点北京对齐。

### Task 3: `worker_loop` 调度化

**Files:** Modify `services/doc-sync-worker/app/pipelines/worker_loop.py`；Test 既有 2 例保持 + 新增 3 例

**Produces:** `run_worker_loop(*, full_sync=None, consume_requests=None, sleep=time.sleep, max_cycles=None, schedule_reader=None, config_puller=None, now_fn=None, last_full_reader=None) -> int`

- 每大轮开头 `schedule_reader()`（默认=`read_schedule_config`）热读；`enabled=False` → 跳过全量、打日志、等待 `min(interval, 600)` 后重读。
- 首轮用 `last_full_reader()`（默认=store 读 `last_full_run_started_at`，异常→None）判断是否到点：now < due → 不跑全量直接进入等待（修"重启即全量"）；due 到 → 跑。之后各轮用进程内上次全量时间。
- 等待期按 poll 步进：sleep→consume_requests→`config_puller()`（默认=`pull_config_from_bitable`，节流 ≥120s 一次，异常吞掉打日志）。
- 等待时长 = next_due - now（anchor 相位对齐）；cycles 计数与 max_cycles 语义不变（一次"全量+等待"=1 cycle；disabled 的空轮也计 cycle，测试可控）。
- 兼容：不传新参数且 DB 不可用时行为=旧版（立即全量+interval 等待+每 poll consume）。

新增测试：disabled 跳过全量仍 consume；last_full_reader 返回"刚跑过"→ 首轮不跑全量；anchor 生效（schedule_reader 注入 anchor+interval，检查 slept 总和=due-now）。

### Task 4: backend 端点 GET/PUT `/v1/ops/doc-sync/sync-config`

**Files:** Modify `services/backend-api/app/routers/ops.py`；Test `tests/test_backend_ops_status.py`

**Produces:**
- `_read_sync_config_row(provider)` 扩展 SELECT anchor_time, pull_paused（列不存在/DB 挂 → 默认 `anchor_time:"", pull_paused:False`）。
- `DocSyncConfigUpdate(BaseModel)`: enabled bool; interval_hours float 1-168; anchor_time str 默认 ""（校验 `^$|^([01]\d|2[0-3]):[0-5]\d$`）; pull_paused bool 默认 False。
- GET 返回 enabled/interval_seconds/interval_hours/anchor_time/pull_paused/updated_at/updated_by/source（updated_by=='feishu-config-table'→"飞书配置表"，空→"默认"，其余→"手动"）。
- PUT upsert 全字段（含 pull_paused），updated_by=当前用户。

测试：GET 无 DB 回默认；模型校验 anchor "9:99" 拒绝、"02:00"/"" 通过。

### Task 5: exports 页设置卡

**Files:** Modify `services/public-web/exports/index.html`；Test `tests/test_exports_frontend.py`

在"数据导出" band 之前加一张卡：开关 checkbox + 周期小时 number + 起点时间 `<input type="time">` + "暂停表格拉取" checkbox + 状态行（来源/更新时间/更新人）+ 保存按钮；`applyGate` 管理员分支追加 `loadDocSyncConfig()`；JS 函数 `loadDocSyncConfig/saveDocSyncConfig` 仿 tplus-sync 页同名函数（api 路径换 `/v1/ops/doc-sync/sync-config`）。前端测试按 test_exports_frontend.py 既有断言风格加存在性断言。

### Task 6: 全量验证 + PR + 部署后 ops

- `python -m pytest tests/ -q` 全绿 → push → PR → CI → 合并（release-deploy：backend/doc-sync-worker/public-web 三镜像变更，迁移 0025 自动跑）。
- 部署后：①`/exports/` 设置卡可读可存；②给飞书「配置表」建表（4 列+3 行 seed，用 ECS 上飞书凭据 API 建或用户手建）；③改表格值→2 分钟内 GET 端点 source 变"飞书配置表"；④重启 worker 确认不再立即全量。

---

## PR③ 任务（bridge）

### Task 7: 规则表文案 4 项

**Files:** Modify `deploy/openclaw-bridge/openclaw_bridge.py`；Test `tests/test_openclaw_bridge.py`

- `feishu_global_rule_policy()`：env_defaults 增 4 文本键 `处理中文案/追问文案/空回复文案/完成标记`（默认=对应 env→DEFAULT_*）；读表循环对 bool 键用 `bitable_truthy`、文本键取字符串（兼容 bitable 富文本 list 段落，取拼接文本；空串视为未设置不覆盖）。
- `ensure_feishu_default_rule_record()`：ensure 列清单 + 新行/回填字段加 4 文本键，值=当前 env 解析后的默认文案（用户打开表格即见当前文案可改）。
- 4 个 getter（`processing_ack_text/processing_remind_text/processing_empty_fallback_text/done_marker_text`）改为先取 policy 非空值，否则原 env/默认逻辑。
- 测试：mock `find_feishu_bitable_record` 返回含自定义文案的行 → getter 返回表格值；表格值空/缺列 → env/默认；bool 开关行为不回归（既有测试）。

### Task 8: 验证 + PR + 手动 cutover

- pytest 全绿 → PR → 合并 → 等 bridge 镜像 V-tag → ECS `docker rm -f openclaw-bridge` + 改 OPENCLAW_BRIDGE_TAG + `compose up -d` → 真机改「处理中文案」发消息验证占位卡文字变化。
