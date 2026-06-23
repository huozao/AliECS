# T+ 同步统一时间线 + 全量版本/Excel + 收窄复核规则 — 设计

日期：2026-06-24
状态：待用户评审
影响仓库：`AliECS`（backend-api / tplus-sync-worker / public-web）

## 1. 背景与现状

health 页（`/health/`）的 `T+ 同步` 区块目前是**两张表、两套 ID**：

- **同步请求**（`integration_sync_requests`）：畅捷通回调或手动触发；可能有 `sync_run_id` 指向某次执行，也可能没有（pending/失败/未跑）。
- **执行记录（全部）**（`integration_sync_runs`）：真正跑过的执行。**formula 页 `/formula/` 的「同步 #251」就是这张表的 `id`。** 定时全量（scheduled_full）只有执行行、没有请求行。

现状的几个关键事实（已核对代码）：

1. **执行记录不存"产出了哪个 Excel"**。formula 页能显示 `来源：bom_xxx.xlsx · 同步 #251`，靠的是约定「按 mtime 取最新文件 ⇔ 最近一次成功执行」（`_latest_bom_sync_run()` 注释明示），二者无显式外键。
2. **增量(订阅)同步只导出"部分 Excel"**：`job_sync_bom.main(target, mode="incremental")` 在 target 带具体 BOM 时只拉那一个 BOM、`export_bom` 只导那一个、`record_bom_snapshot_if_configured(mode="incremental")` 只存那一个，且**增量不算 diff、不报警**——只有 `full_bom`/`scheduled_full` 才比对上一份全量快照。
3. **全量快照只要 hash 一变就报 `needs_review`（warning）**（`build_snapshot_diff`），不分变化类型，所以停用/改名也会报警。
4. item 的 `record_key` 里**含 `disabled`**（`_bom_item_from_parent_child`），所以"启用→停用"现在表现为"删旧 key + 增新 key"，会被误当作大改动。
5. Excel 文件：`{module}_{YYYYMMDD}_{HHMMSS}.xlsx`，目录 `TPLUS_EXPORT_DIR`（默认 `/app/tplus-output/excel`）；下载走 `GET /v1/exports/tplus/{file_name}`（admin）；**每类按 mtime 只保留最新 48 个**（`retention.py`，更早的被清理，无法下载）。

## 2. 目标

1. 把「同步请求」+「执行记录」合并成**一张统一编号的时间线表**，加一列「生成的 Excel」可点下载，与 formula 页的 `bom_xxx.xlsx · 同步 #N` 一一对应、可核对。
2. **每次 BOM 同步（手动全量 / 定时全量 / 增量订阅）都产出一份全量 BOM Excel + 一条全量快照**，让时间线每一行都能下载"当时完整 BOM"。
3. **收窄"需人工复核"判定**：只在①数值变化②原料种类增删/替换③整条 BOM 删除时才需复核；其余（停用/启用、默认/非默认、新增整条 BOM、名称/单位/备注/损耗率变化）只记录、不报警。
4. **弱化"差异校验"独立区块**：把复核动作内联进需要复核那一行的详情里。

## 3. 已定决策（用户确认）

| # | 决策 | 选择 |
|---|------|------|
| D1 | 时间线行的口径 | **执行为主 + 并入孤儿请求**（run 行 + `sync_run_id IS NULL` 的请求行，按时间排序统一编号） |
| D2 | 统一编号 | **直接用 run id**（对齐 formula `#251`）；孤儿请求行显示 `请求·R<req_id>`（弱化、暂无 Excel） |
| D3 | Excel 关联 | **混合**：优先读 `detail_json.export_files`，没有则回退按文件名时间戳匹配 |
| D4 | 多文件显示 | **列出该次执行产出的全部文件**，每个一个下载链接（chip） |
| D5 | 全量 Excel 策略 | **从数据库拼全量**：增量先 upsert 进 `tplus_bom_records`，再从该表导出全量 Excel + 全量快照，不额外全量拉 T+ |
| D6 | 复核触发集合 | **恰好三类**：①quantity 变化 ②child 物料种类增删/替换 ③整条 BOM 删除 |

## 4. 架构总览：统一的"同步后处理"流程

无论手动全量 / 定时全量 / 增量订阅，BOM 同步成功后都走同一条后处理：

```
拉取(全量或增量) → upsert 进 tplus_bom_records
  → 从 tplus_bom_records 组装"当前全量集" current_full
  → snapshot_bom_rows(current_full) 得全量快照 S_cur
  → 与上一份全量快照 S_prev 做 diff，按 §5.3 分类
      · 含复核类变化 → 写 needs_review reconciliation diff
      · 仅 informational → 不写 needs_review（diff 摘要仍随 run 记录）
  → export_bom(current_full) 导出全量 Excel（文件名 F）
  → 记录 integration_sync_runs：detail_json.export_files = [F, ...]，
    detail_json.diff_summary = {added, removed, qty_changed, material_changed, bom_deleted, status_changed, cosmetic_changed, needs_review:bool}
```

这统一了语义：**每次同步都有全量快照 + 全量 Excel + 一次分类后的 diff**。增量与全量只在"拉取范围"上不同，后处理一致。

## 5. P2 · 同步语义（改 tplus-sync-worker）

### 5.1 每次同步产出全量集（从 DB 拼装，D5）

- 新增 helper（如 `assemble_current_full_bom(conn)`）：从 `tplus_bom_records` 读出所有未失踪（`missing_since IS NULL`）记录的 `raw_json`，按 `(Code, Version)` 去重（保留启用态优先 / 最新 last_seen），返回与 T+ API 行同构的 `rows`，供 `export_bom` 与 `snapshot_bom_rows` 复用。
- `job_sync_bom.main`：
  - `incremental` + 有 target → 仍只拉变更 BOM、`_upsert` 进表（沿用现逻辑），**但导出与快照改用组装出的 `current_full`**。
  - 全量模式 → 拉全量、upsert，导出/快照同样用 `current_full`（与现状等价，行为不变）。
- **风险点**：`tplus_bom_records` 的 upsert key 含 `Disabled`（`_record_key`），停用会产生新 key、旧启用行残留 → 组装全量需按 `(Code, Version)` 去重；缺失清理（missing sweep）只在全量同步发生，增量不会把"别处删掉的 BOM"标失踪。组装出的全量是"上次全量 + 期间增量"的最佳已知状态，可接受（见 §9）。

### 5.2 全量快照 + diff（统一）

- `record_bom_snapshot_if_configured` 改为：**所有 mode 都按全量快照处理**（不再只 `full_bom`/`scheduled_full` 才 diff）。即增量也写一条 `module='bom'` 的全量快照并与上一份全量快照 diff。
- `mode` 字段保留以区分来源（手动/定时/增量），但 diff/复核逻辑不再因 mode 而异。

### 5.3 变化分类与复核规则（D6）

基于 item 级 diff（`_diff_snapshot_items`）产出的 added / removed / changed，归类：

- **bom_deleted（复核）**：某 `(parent_code, version)` 在 previous 存在、在 current 完全不存在（其全部 child 都 removed，且该 parent 没有任何 current item）。注意要与"停用"区分（停用时 parent 仍在，仅 `disabled` 翻转）。
- **material_changed（复核）**：同一 `(parent_code, version)` 下 child 物料集合变化——新增 child、删除 child、或 child_code 被替换。
- **qty_changed（复核）**：common key 的 changed_fields 含 `quantity`。
- **status_changed（informational）**：仅 `disabled` 或 `default_bom` 变化。
- **cosmetic_changed（informational）**：仅 `parent_name` / `child_name` / `unit` / `memo` / `waste_rate` 变化。
- **bom_added（informational）**：current 出现全新 `(parent_code, version)`。

判定：`needs_review = (qty_changed > 0) or (material_changed > 0) or (bom_deleted > 0)`。仅当 `needs_review` 时写 `integration_reconciliation_diffs`（status='needs_review'）；否则不写，避免污染"需要关注"。`diff_summary`（各类计数 + needs_review）写进 `integration_sync_runs.detail_json`，供时间线行内展示。

### 5.4 record_key / disabled 处理（D6 前置）

- item `record_key` **去掉 `disabled` 段**（改为 `parent_code|version|child_code|child_id`），使"停用↔启用"表现为同 key 的 `disabled` 字段 changed（→ status_changed/informational），而不是 removed+added。
- `disabled` 仍保留在 `comparable` 里参与 hash，以便被检测为"changed 且 changed_fields=['disabled']"。
- `default_bom` 已在 `comparable`、不在 key，无需改。
- `tplus_bom_records` 的表级 upsert key 是否同步去掉 `disabled` 留作实现期评估（影响 §5.1 去重；倾向去掉 `Disabled` 让停用直接覆盖同一行，最干净，但需评估缺失清理与历史数据）。

### 5.5 export_files 写入 run（D3 精确侧）

- 全量/增量/手动各路径在 `record_*sync_run*` 时把产出文件名写进 `detail_json.export_files`（list）。
- 定时全量（`worker_loop` → `sync_once`）会产出多模块文件（bom/库存/价格…）：让编排把各模块产出的文件名汇总上来，写进 `export_files`（D4 多文件）。具体汇总方式在实现计划里定（倾向各 export job 返回路径、orchestrator 聚合）。

## 6. P1 · 统一时间线（改 backend-api + public-web）

### 6.1 新端点 `GET /v1/ops/tplus/timeline?limit&offset`（admin）

返回按时间倒序、分页的统一行。SQL 用 `UNION ALL`：

- **run 行**：`integration_sync_runs`（沿用现 `/runs` 的 LATERAL join 取触发请求的 `id`/`reason_event_id`），`event_time = finished_at`，`kind='run'`，`number = '#'||sr.id`。
- **孤儿请求行**：`integration_sync_requests WHERE sync_run_id IS NULL`，`event_time = requested_at`，`kind='request'`，`number = '请求·R'||r.id`。

外层 `ORDER BY event_time DESC NULLS LAST` + `LIMIT/OFFSET`；`total` 为两者计数之和。旧端点 `/v1/ops/tplus/runs`、`/v1/ops/tplus/requests` **保留不动**（降低风险，formula 路径不受影响）。

每个 run 行附加：
- `export_files`: `[{name, download_url|null, pruned:bool}]`——优先 `detail_json.export_files`；缺失则回退 §6.2 时间匹配；文件不在磁盘（被保留策略清理）→ `download_url=null, pruned=true`。
- `diff_summary`: 取自 `detail_json.diff_summary`（无则省略，历史行不显示变化摘要）。
- `needs_review` 与对应 `reconciliation_id`（若有），供行内复核入口。

### 6.2 Excel 时间匹配（D3 回退侧，覆盖历史行）

后端读 `TPLUS_EXPORT_DIR` 目录清单，按文件名解析时间戳 `t_F`；把每个文件归给"finished_at ≥ t_F 的最早一次 run"（runs 与 files 都按时间排序后桶分配，每文件唯一归属）。module='all' 定时执行 → 归到 bom/库存/价格等多个文件；手动 bom → 归到 bom 一个。只有仍在磁盘的可下载。

### 6.3 health 页表格与列

用一张表替换原「同步请求」「执行记录（全部）」两张表（区块标题仍为 `T+ 同步`）。列：

`编号 · 来源 · 模块 · 模式 · 状态 · 时间 · 行数 · 退出码 · 回调事件ID · 本次变化 · 生成的 Excel · 详情`

- **编号**：run 行 `#<id>`；孤儿请求行 `请求·R<id>`（灰）。
- **来源**：复用 `syncOriginLabel`（手动/定时/订阅变更 chip）。
- **本次变化**：来自 `diff_summary` 的简短计数（如 `改数值2 · 增料1`）；需复核 → 行高亮（warn/critical）。无摘要 → `—`。
- **生成的 Excel**：`export_files` 每个一个 chip + 「下载」（复用现有 `downloadExport` 与 `/v1/exports/tplus/{file}`）；`pruned` → 灰字「已清理」；失败/无产出 → `—`。
- **详情**：弹窗合并原请求/执行详情（target_json/detail_json/error_json + 请求ID/执行ID + diff 明细）；当 `needs_review` 时，在详情里提供原复核动作（采用当前快照/保留上一快照/忽略，复用 `/v1/ops/reconciliation/{id}/actions`）。

### 6.4 差异校验弱化（目标4）

- 删除独立的「差异校验」大区块（或缩为一个"待复核 N 条"的小提示，点开滚动/筛选到时间线中需复核的行）。
- 复核处理动作迁移到时间线行详情里（见 6.3）。`reconciliation` 后端端点保留不动。

## 7. 数据流（时间线一行的生命周期）

1. 同步发生 → §4 后处理 → 写 `integration_sync_runs`（含 `detail_json.export_files`/`diff_summary`），需复核则写 `integration_reconciliation_diffs`。
2. 前端 `GET /v1/ops/tplus/timeline` → 后端合并 runs+孤儿请求、附 export_files（detail_json 优先、回退匹配）、diff_summary、needs_review。
3. 前端渲染一张表；点「下载」→ `GET /v1/exports/tplus/{file}`；点「详情」→ 弹窗（含复核动作）。
4. 与 formula 页对应：formula 的 `同步 #N` 即时间线 `#N` 行；该行「生成的 Excel」即 formula 的 `来源：bom_xxx.xlsx`。

## 8. 范围外 / 不做

- 不改 formula 页查询逻辑与 `_latest_bom_sync_run()`（仅保证编号/文件名能对上）。
- 不动非 BOM 模块（库存/价格等）的同步语义；它们的 Excel 仍照常进时间线的 export_files（定时全量行）。
- 不引入新依赖、不改部署/CI 配置。
- 不做历史执行的 `export_files` 回填（历史行靠 §6.2 时间匹配）。

## 9. 风险

- **DB 拼装全量的完整性**：增量不跑缺失清理，"别处被删的 BOM"要等下次全量才反映。组装全量=上次全量+期间增量的最佳已知状态，可接受；删除类的权威判定仍以全量同步为准。
- **停用残留行**：若 `tplus_bom_records` 表级 key 保留 `Disabled`，停用会留旧启用行，§5.1 去重必须可靠，否则全量 Excel 出现重复行。实现期需测。
- **时间匹配边界**：同一秒多文件、worker 与 DB 时钟差可能让回退匹配偶发错配；精确侧（`export_files`）落地后新行不受影响，历史行属尽力而为。
- **保留策略**：超出每类最新 48 个的历史行无法下载（显示"已清理"），符合预期。

## 10. 测试（红绿）

- worker：`assemble_current_full_bom` 去重/失踪过滤；统一后处理对 incremental 也产全量快照+全量 Excel；`detail_json.export_files` 写入。
- diff 分类：qty/material/bom_deleted → needs_review；disabled/default/新增BOM/名称单位备注损耗率 → informational 不报警；停用经 record_key 调整后识别为 status_changed 而非 delete+add；BOM 删除 vs 停用 的区分。
- backend：timeline 合并/分页/排序（run+孤儿请求混排）；export_files 取数（detail_json 优先、回退时间匹配，含 all 多文件、pruned 标记、边界）。
- 前端：沿用现有 `tests/test_health_frontend.py` 风格补断言（统一表存在、列、下载/详情、复核入口）。

## 11. 部署

- AliECS 走 **PR + 本地 pytest**（用户硬规则）。worker 改动需 build 镜像并部署 tplus-sync-worker；backend/public-web 随 release 部署。
- 分支：`feature/tplus-sync-timeline-unified`。
