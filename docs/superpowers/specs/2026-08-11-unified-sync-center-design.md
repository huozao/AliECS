# 统一同步平台中心 设计（2026-08-11）

> 状态：设计已确认，待实施。分 P0–P5 六段执行，每段可独立回滚。
> 本文是跨 session 的交接载体：新 session 冷启动接手时，读本文 + 第 11 节文件清单即可继续。

## 1. 背景：不是页面不好看，是底下裂成了两套

同一个「同步」概念在仓里有两套彼此独立的模型、配置面和页面。任何改进都要做两遍，且两边能力永远不对齐。

| | doc-sync（企微/飞书） | T+（畅捷通） |
|---|---|---|
| 源登记 | `external_sources` | 无，硬编码 module |
| 数据缓存 | `external_records` | `tplus_bom_records` / `integration_sync_snapshots` |
| 运行记录 | `sync_runs` | `integration_sync_runs` |
| 手动触发 | `sync_requests` | `integration_sync_requests` |
| 调度配置 | `system_config` + 飞书配置表覆盖 + `pull_paused` | `integration_sync_config` |
| 差异明细 | 无 | `integration_reconciliation_diffs` |
| 页面 | `/exports/` | `/tplus-sync/` |
| 失败告警 | 无，只写 `error_json` | `ops.py` 两个后台线程 |

两个页面的 CSS 与 `api()` / `applyGate()` / `downloadExport()` 是逐字复制的两份。

### 1.1 现有的四个具体缺口

1. **看不到"卡在哪一步"**：run 级别只有成功/失败/退出码，分不清是取 token 失败、分页第 7 页 429、还是写库失败。
2. **看不到"数据是不是旧的"**：没有任何机制能发现"某个源三天没同步成功"。这比"某次跑失败了"重要得多。
3. **告警是散点**：`ops.py` 两个 loop + `tplus_parent_match.py` 自推 + `gold_spread_alerts.py` 又一套。每加一个源就再写一个 loop；企微/飞书同步失败**完全没有告警**。
4. **手动同步后是盲区**：触发到出结果的 30~120 秒里，页面靠定时盲刷（`tplus-sync/index.html:100` 注释自认"立刻刷新时间线是空的"），分不清还没开始 / 正在跑 / 已失败。

## 2. 目标与非目标

**目标**：一个 `/sync/` 页面回答四个问题——现在有没有问题、数据是不是新鲜的、这次跑卡在哪一步、出问题有没有人被通知到。

**非目标（明确不做）**：
- 不搬业务数据。`tplus_bom_records`、`external_records`、`tplus_inventory_records` 一行不动。
- 不建中央调度器服务。执行权仍在各 worker 进程内。
- 不碰 `/formula/` 的任何读取路径（详见第 8 节）。
- 不做告警卡片，纯文本（`lark_md` 限制多，维护成本不值）。

## 3. 已拍板决策

| 决策 | 结论 |
|---|---|
| 底层数据模型 | **统一**，页面在其上重建；旧表保留一段时间只读 |
| 缓存边界 | 只统一元数据，业务数据各归各 |
| 建模粒度 | 建模为「同步作业 job」，一期接四类：企微、飞书、T+ 全量、T+ 父件核对 |
| 告警策略 | 状态变化触发 + 升级间隔，不逐轮刷 |
| 页面归属 | `/sync/` 管过程；`/exports/` 瘦身为纯数据出口；`/tplus-sync/` 下线重定向 |
| 调度执行权 | **搬**，但必须有搬前基线 + 影子期 + 搬后对比 |
| T+ 无锚点行为变化 | **接受**"重启不再立即全量"（统一采用 doc-sync 语义） |

## 4. 数据模型：4 张新表

前缀统一 `sync_job_*`，与现有 `sync_runs` / `integration_sync_runs` 不撞名。

### `sync_jobs` — 作业登记，平台的目录

```
job_key TEXT UNIQUE       -- 'wecom.doc.<source_id>' / 'feishu.doc.<source_id>'
                          -- 'chanjet.full' / 'tplus.parent_match'
kind                      -- pull | writeback | reconcile
provider, display_name
source_id → external_sources(id)    -- 仅 pull 类有
enabled
schedule JSONB            -- {interval_seconds, anchor_time}；P4 后是调度内核的唯一配置面
freshness_sla_seconds     -- 多久内必须有一次成功；NULL = 不判新鲜度
artifact_glob TEXT        -- 产出物匹配式，用于产出物新鲜度（见第 8 节）
alert_enabled, alert_chat_id        -- 留空走 env SYNC_ALERT_CHAT_ID
```

### `sync_job_runs` — 每次执行

```
job_id, trigger(schedule|manual|event), status(running|success|partial|failed)
started_at, finished_at, row_count, changed_count
error_kind                -- auth | rate_limit | network | schema | write | unknown
error_message, detail_json
legacy_ref JSONB          -- {table:'integration_sync_runs', id:123}，双写期回指追溯
```

`error_kind` 是"看到问题在哪"的第一层：页面和告警直接说**"凭据过期"**，而不是丢一段 traceback 让人猜。

### `sync_job_steps` — 步骤，现在完全缺失的一层

```
run_id, seq, name(token|list_sheets|fetch_page|normalize|upsert|writeback)
status, started_at, finished_at, items, message
UNIQUE INDEX (run_id, seq)
```

索引唯一是有意的：P1 按 `(run_id, seq)` upsert 步骤状态（running → success/failed），
不唯一就没有冲突键，只能先查后写，并发下会插出重复步骤。

保留策略：成功 run 的 steps 保留 30 天，失败 run 保留 90 天。清理挂在 notifier 同一个 loop 里。

### `sync_job_alerts` — 告警状态机

```
job_id, run_id(可空，FK → sync_job_runs ON DELETE SET NULL)
alert_kind(failed|stale|credential_expiring|artifact_stale)
state(open|resolved) CHECK 焊死, first_seen_at, last_notified_at, notify_count, resolved_at
payload_json
UNIQUE partial index (job_id, alert_kind) WHERE state='open'
```

那条 partial unique index 就是防刷屏的根：一个作业一种告警，同时只可能有一条 open。

抢占的确定写法（偏索引必须带谓词才能完成 index inference，写成
`ON CONFLICT (job_id, alert_kind)` 不带 `WHERE` 会直接报 42P10）：

```sql
INSERT INTO sync_job_alerts (job_id, run_id, alert_kind, payload_json)
VALUES ($1, $2, $3, $4)
ON CONFLICT (job_id, alert_kind) WHERE state = 'open' DO NOTHING
RETURNING id;          -- 返回 0 行 = 抢占失败 = 已有人推过，本次不推
```

不要用无 target 的 `ON CONFLICT DO NOTHING`：它会吞掉本表**任何**唯一索引的冲突。

`state` 由 `CHECK (state IN ('open','resolved'))` 焊死——去重正确性完全骑在这个字面量上，
写成 `'OPEN'` 不报错、只会静默绕过偏索引导致同一告警无限刷屏。

`run_id` 可空是有意的：`artifact_stale`、长时间没跑这类新鲜度告警本就没有对应的 run。

### 4.1 两条边界

- **业务数据不搬**：平台只存指针（表名 + 行数）。formula 页、BOM builder、父件核对零改动。
- **一期双写不删旧表**：worker 在原有写入点后**追加**新表写入，`legacy_ref` 保留追溯。旧表、旧页面 API 保持可用。

## 5. 调度统一与搬迁

### 5.1 真实起点：算法已同源，但是两份复制

`doc-sync/app/pipelines/sync_schedule.py:117 next_full_sync_due()` 与
`tplus-sync-worker/src/tplus_datahub/jobs/worker_loop.py:125 next_scheduled_full_due()`
是逐行同源实现，T+ 侧注释明写"与 doc-sync 的 next_full_sync_due 同一套语义"。

所以搬迁不是从零建调度器，是**把两份复制收敛成一份并补齐不对称能力**。

### 5.2 三处实质不对称

**① 无锚点时的到期判断（唯一会改变生产行为的点）**
- T+ `worker_loop.py:346`：`not_due = enabled and bool(anchor_time) and due > current` — 只有设了锚点才判到期，无锚点时容器重启即跑全量。
- doc-sync `worker_loop.py:155`：无条件判到期，重启不重跑（注释写明是修"重启即全量"）。

**已拍板**：统一采用 doc-sync 语义。若 T+ 生产实际未设锚点，切换当天行为会变（重启不再立即全量），此变化已接受。搬前必须上机器读一次 `integration_sync_config` 实际值确认影响面。

**② 热唤醒只有 T+ 有**
T+ `worker_loop.py:418 _schedule_target_moved_earlier()` 在睡眠中每个 poll 片热读配置，目标被改早就退出睡眠。该函数带 2026-08-11 实测注释："改成 01:00 后当晚整轮没跑，连失败记录都没有"。
doc-sync 没有此机制，同一个坑原样存在。搬迁后 doc-sync 白捡这个能力，是收益之一。

**③ 配置来源不对称**
doc-sync 的配置能被飞书「同步配置」表覆盖（`pull_config_from_bitable`，每 120s，带 `pull_paused` 应急开关），T+ 没有。统一后需决定该拉取器是升级为全局能力还是保持仅 doc-sync 生效。**默认保持仅 doc-sync**，避免扩大影响面。

### 5.3 搬法：共享内核 + 统一配置面

新增 `sync_scheduler` 模块（算 due、判 run_full、算 wait_seconds、热唤醒判据）。两个 worker 各自 `COPY` 同一份源文件，加一致性断言测试（沿用隧道配置"共享文件两 role 同装 + verify 断言"的做法）。配置面统一到 `sync_jobs.schedule`。

**不建中央调度进程**：那意味着 worker 定时路径要删掉、全靠新服务投递，新服务一挂全部同步停摆，为一点集中度换一个新单点，不划算。

### 5.4 搬前基线（≥14 天，覆盖两个以上周期）

从 `sync_runs` 与 `integration_sync_runs`（`mode='scheduled_full'`）导出五项：

| 指标 | 用途 |
|---|---|
| 每次定时全量的实际开始时刻（北京时间 HH:MM）序列 | 锚点漂移 |
| 相邻间隔秒数 min/median/max | 「实际睡眠秒数」的历史证据 |
| 每周期执行次数（应恒为 1） | >1 重跑，0 漏跑 |
| 成功率、时长、行数 | 搬迁不该动这些 |
| 手动/事件触发次数 | 对照组 |

同时上机器读两边配置实际生效值（尤其 T+ 有无锚点）。

### 5.5 影子期：把"验睡眠秒数"变成连续 7 天的自动证据

新内核先以 `SYNC_SCHEDULER_MODE=shadow` 上线：每轮**同时**用新内核算一遍决策，只写 `sync_job_runs.detail_json.shadow`，**不改变任何实际执行**。旧逻辑照常驱动生产。

跑满 7 天比对三条断言：
1. 每轮新旧 `run_full` 决策完全一致 —— 分歧数必须为 0
2. 每轮新旧算出的 `due` 差 ≤ 30 秒
3. 新内核算出的 `wait_seconds` 与实际观测睡眠秒数一致

分歧全部归零才允许切 `active`。

这条直接对应「改调度必须验实际睡眠秒数」的历史教训（同一处踩过两次），把"改完人肉看一眼"升级成"连续七天机器比对"。

另需**等价性单测**：同一组输入（跨周期、跨天、间隔非整除 24h、无锚点、`last_full` 恰等锚点）跑新内核与两份旧实现，断言三者输出相同。影子期是动态等价证明，单测是静态等价证明，两个都要。

### 5.6 切换、回滚与验收

- env 开关 `SYNC_SCHEDULER_MODE=legacy|shadow|active`，**逐 worker 切**，不同时切两个。
- 回滚 = 改回 `legacy` 重启，不涉及数据库回滚。
- 切后再采同口径基线 7 天，与搬前逐项对比。

验收断言：
- 锚点命中：实际开始时刻 − 配置锚点 ≤ poll 间隔(30s) + 上一轮全量时长
- 每周期恰好 1 次，无重跑无漏跑
- **改配置生效时延 ≤ 1 个 poll 周期**（doc-sync 搬后新获得的能力，必须实测，不能只看代码）

## 6. 告警 notifier

### 6.1 放 doc-sync-worker，不放 backend

新增 `services/doc-sync-worker/app/pipelines/sync_alert_notifier.py`，挂进 run-loop 的 30s poll 周期。

理由两条：backend-api 多副本会重复推（现有 `ops.py` 两个线程就有此隐患）；`docs/constraints/doc-sync.md` 第 9 条写死 backend 只查询不调外部 API。

### 6.2 每轮六件事

1. 某 job 最近一次 run 是 failed/partial 且无 open 的 failed 告警 → 开一条并推送
2. open 告警距 `last_notified_at` > 升级间隔（默认 6h）→ 再推汇总，`notify_count+1`
3. 某 job 有 SLA 且 `now - last_success_at > SLA` 且无 open 的 stale 告警 → 开一条并推送
4. 某 job 有 `artifact_glob` 且产出文件 mtime 明显落后于最后成功 run → 开 `artifact_stale`（见 8.3）
5. 凭据将过期 → 开 `credential_expiring`。**接管 `ops.py` 现有的 T+ openToken 告警**，判据沿用原 loop（T+ openToken 6 天有效期，靠 webhook 续命，过期后恢复动作在开放平台侧不在服务器）
6. open 告警对应 job 出现新的 success（或凭据已刷新）→ resolve + 推恢复

第 5 条是 6.4 下线 `_chanjet_token_alert_loop` 的前提：先接管，再下线，不能反过来。

幂等靠"`INSERT ON CONFLICT DO NOTHING` 抢占成功才推"，照抄 `gold_spread_alerts.py:750 _claim_alert()`，不新发明。

### 6.3 消息形态

纯文本：

```
⚠️ 同步失败：企微·<表格名>
原因：凭据过期(auth)
最后成功：08-10 02:14（32 小时前）· 连续失败 3 次
https://hydwang.xyz/sync/?job=<job_key>
```

`error_kind` 翻成中文短语，这是该字段的兑现点。

### 6.4 必须同批下线

- `ops.py` 的 `_chanjet_token_alert_loop`、`_tplus_full_sync_alert_loop` — 不下线就是双推。
- `tplus_parent_match.py` 自身那条推送**保留**（推的是业务核对结果，不是故障告警，性质不同），但它**自身失败**改由 notifier 统一报。

### 6.5 群 ID

全局默认走 env `SYNC_ALERT_CHAT_ID`，`sync_jobs.alert_chat_id` 可按作业覆盖。
**AliECS 是公开仓库，真实 chat_id 不写进仓库**，值走 SOPS 渲染。

## 7. 页面

### 7.1 `/sync/` 三层

**首屏总览**（现在两页都没有的一层）：
汇总行 `N 个作业 · N 新鲜 · N 过期 · N 失败 · N 条未处理告警`；
每作业一行：名称 / provider / 最后成功时间 / **新鲜度**（新鲜·临期≥SLA 80%·过期）/ 最近状态 / 下次预计 / `[立即同步]` `[编辑]`。

**中层时间线**：沿用 `/tplus-sync/` 现有形态（编号+来源+状态+行数+变化摘要，做得不错），加 provider 与状态筛选，覆盖全部作业。

**详情抽屉**：`error_kind` 中文短语打头 → steps 瀑布（名称、耗时、条目数、错误）→ 差异明细。T+ 复用现有 `reconciliation_id`；其他源一期只显示行数变化。

**手动同步的过程可见**（解决第 1.4 条缺口）：触发即建 `status=running` 的 run，页面 3s 轮询 run + steps：

```
排队中 → 取 token ✓ 0.4s → 列出工作表 ✓ 12 张 → 拉取 3/12 ⏳ → …
```

失败停在出错步并带 `error_kind`。不上 SSE/WebSocket，轮询足够。

### 7.2 API

```
GET  /v1/sync/overview               作业列表 + 健康 + 新鲜度
GET  /v1/sync/jobs/{key}/runs        时间线，分页
GET  /v1/sync/runs/{id}              run 详情，含 steps
POST /v1/sync/jobs/{key}/run         手动触发，内部路由到各自现有 request 表
PUT  /v1/sync/jobs/{key}/config      写回 sync_jobs
GET  /v1/sync/alerts                 告警列表
```

### 7.3 旧页面处置

- `/exports/` 删掉「同步设置」整块与「立即同步」按钮，只留导出目录/下载/创建副本。
- `/tplus-sync/` 由 nginx 301 → `/sync/?job=chanjet.full`。

### 7.4 顺手必做的清理

两页现有 CSS 与 `api()`/`applyGate()`/`downloadExport()` 是逐字复制两份；不抽出来新页面就是第三份。抽到 `services/public-web/common/admin.css` + `common/admin-auth.js`（`common/toast.js` 已有先例）。

## 8. `/formula/` 影响分析与产出物新鲜度

### 8.1 formula 的数据来源是分裂的两条

**配方查询 + 成本核算 → 读文件系统上的 Excel，不读数据库**
- 配方：`app/recipes/bom_query.py:190 locate_recipe_source()` → `RECIPE_BOM_INPUT_DIR`（默认 `/app/tplus-output/excel`），glob `*物料清单合并*.xlsx`，取最新一个文件
- 成本：`app/recipes/price_lookup.py:27` → `TPLUS_EXPORT_DIR`（同目录），`purchase_price_*.xlsx` / `sales_price_*.xlsx`，取最新

**颜色 / Lab / 父件信息 → 读数据库**
- `app/routers/formula_colors.py:80` — `external_records` LEFT JOIN `tplus_bom_records` + `tplus_inventory_records`

### 8.2 一个现有的静默风险

**T+ 同步失败时，formula 不报错，它静默继续用上一次的旧 Excel。** 页面照常出结果、成本照常算，只是数据是旧的，且没有任何地方会提示。现有 `/tplus-sync/` 只显示 run 成功/失败，不显示"formula 此刻实际在读哪个文件、那文件是几点的"。

`prune_exports.py` 每类保留 48 份（`TPLUS_EXPORT_RETENTION` 默认 48），按每天一次全量约 48 天，文件被清空风险不大；"读到旧文件而不自知"的风险则天天存在。

### 8.3 因此补一块：产出物新鲜度

只盯 run 的 freshness 不够 —— run 成功 ≠ 产出物更新了。模块级独立容错下，整轮 `status=success` 但 bom 模块没出文件是可能的（`worker_loop.py:386` 注释正为此场景而写）。

- `sync_jobs.artifact_glob`；run 结束时把本次产出文件名与 mtime 写入 `sync_job_runs.detail_json.artifacts`
- `/sync/` 首屏对 T+ 作业额外显示：**`formula 当前读取：物料清单合并_20260811.xlsx（今天 02:14）`**
- 文件时间明显落后于最后成功 run 时，开 `artifact_stale` 告警

### 8.4 改动风险：零

DB 那半由「业务数据不搬」覆盖；文件那半平台只**读** mtime 展示，不碰产出目录、不碰 prune 脚本、不碰任何 `RECIPE_*` / `TPLUS_EXPORT_DIR` 环境变量。`/formula/` 查询链路全程不受影响，P0–P5 每段都不需要动它。

## 9. 分阶段实施与回滚

| 阶段 | 内容 | 回滚 |
|---|---|---|
| P0 | 建 4 张表 + 抽 common 前端资产 | 无行为变化 |
| P1 | 两个 worker 双写 runs/steps，旧写入点保留，`legacy_ref` 回指 | 撤双写 |
| P2 | `/sync/` 只读上线，旧两页不动 | 下线新页 |
| P3 | notifier 上线 + `ops.py` 两线程下线 | 恢复两线程 |
| P4 | 调度内核 shadow 7 天 → 逐 worker 切 active | 改回 `legacy` 重启 |
| P5 | `/tplus-sync/` 重定向、`/exports/` 瘦身 | 撤 nginx 规则 |

- P2 的价值是新旧并存一段时间，可直接对照验证新表数据与旧表一致。
- P4 与 P0–P3 正交，可并行准备，但**不要与 P3 同批上线** —— 告警和调度同时改，出问题分不清是谁的锅。
- P4 含 7 天影子期，天然跨 session。

## 10. 验证要求

- **单测**：调度内核三份实现等价性；due 边界（跨周期/跨天/间隔非整除 24h/无锚点/`last_full` 恰等锚点）；notifier 状态机（开→升级→恢复→再开）；抢占并发；freshness 与 artifact freshness 判定
- **集成**：`local/docker-compose.local.yml` 起 worker + pg，注入假 provider 跑一轮，验证 run/steps 落库
- **前端**：新增 `tests/test_sync_frontend.py`；同时改 `tests/test_exports_frontend.py`（P5 删功能后旧断言会挂）
- **通用**：`python -m unittest discover -s tests`；`docker compose -f local/docker-compose.local.yml config`
- **部署**：按 `docs/runbooks/deploy.md`，判据是 `stage-business-cn-peer` job + 容器双证据

## 11. 冷启动接手指引（跨 session 用）

接手任一阶段前必读本文 + 下列文件：

| 关注点 | 文件 |
|---|---|
| doc-sync 调度与配置 | `services/doc-sync-worker/app/pipelines/sync_schedule.py`、`worker_loop.py` |
| T+ 调度 | `services/tplus-sync-worker/src/tplus_datahub/jobs/worker_loop.py` |
| 现有 T+ 页面 API | `services/backend-api/app/routers/ops.py`（timeline / sync-config / full-sync / 两个告警 loop） |
| 现有导出页 API | `services/backend-api/app/routers/exports.py` |
| 告警抢占模式参考 | `services/backend-api/app/routers/gold_spread_alerts.py:750` |
| formula 读取链路 | `app/recipes/bom_query.py:190`、`app/recipes/price_lookup.py:27`、`app/routers/formula_colors.py:80` |
| 同步表结构约束 | `docs/constraints/doc-sync.md` |
| 现有迁移 | `db/migrations/0005_doc_sync.sql`、`0008_event_driven_sync_health.sql`、`0017_integration_sync_config.sql` |

每阶段完成后在本文末尾追加一行实施记录（阶段、日期、PR、验证结论），供下一 session 判断进度。

## 12. 实施记录

| 阶段 | 日期 | PR | 验证结论 |
|---|---|---|---|
| P0 建表 + 抽 common 前端资产 | 2026-08-12 | [#294](https://github.com/huozao/AliECS/pull/294) | `unittest discover -s tests` 646 项全绿（基线 640）；两页 smoke 通过：`admin.css` 生效（`.btn` 圆角 999px）、`window.AliECSAdmin` 13 个契约字段齐、**零 pageerror 零 console error**、SSO 跳转正常；`check_navigation.py` 通过；迁移 0048 由 CI `migration-dry-run` 在 postgres:16-alpine 上实际执行，全 job 日志零 ERROR/FATAL。**未验证重复执行**（CI 只在全新库上跑一遍，`psql` 未带 `ON_ERROR_STOP`，可重复性目前只有 `IF NOT EXISTS` 与文本断言两层保障） |
| P1 两个 worker 双写 runs/steps | 2026-08-12 | [#299](https://github.com/huozao/AliECS/pull/299) | 企微、飞书、`chanjet.full`、`tplus.parent_match` 已接入 fail-open 双写并保留 legacy 写入；根 unittest 705 项 exit 0（3 skipped），T+ 子项目 158 项全绿，PostgreSQL 16 全迁移集成 1 项通过且测试数据清理为 0；导航与 Compose config 通过；全分支终审无 open Critical/Important。已 squash 合并为 `341df950`；发布 run [31583374908](https://github.com/huozao/AliECS/actions/runs/31583374908) 的 `stage-business-cn-peer` success（`deploy-business-cn` skipped 为正常），txecs 两个 worker 已换新镜像。生产真实执行后：企微 9 个 success run、飞书 13 个、T+ 全量 2 个、父件核对 2 个；文档 22/22 回指 `sync_runs`，manual full 回指 `integration_sync_runs` 且旧行存在。 |

P2 接手须知：

- P1 已让两个 worker 双写 `sync_jobs` / `sync_job_runs` / `sync_job_steps`；P2 只读这些表，旧 `sync_runs` / `integration_sync_runs` 仍是业务写入真源，禁止在 P2 改写或删除 legacy 链路。
- 前端共享资产的对外契约见 `services/public-web/common/admin-auth.js` 末尾的
  `global.AliECSAdmin = {...}`，P2 的 `/sync/` 页直接按它调用。`applyGate(me, onAdmin)`
  的 DOM id 契约是 `loginBtn` / `logoutBtn` / `adminContent` / `gateHint` / `refreshBtn`（可选）。
- 计划文档里写的 `python -m unittest tests.<module>` 在本仓跑不通（`tests/` 无
  `__init__.py`），正确写法是 `python -m unittest discover -s tests -p "test_xxx.py"`。
