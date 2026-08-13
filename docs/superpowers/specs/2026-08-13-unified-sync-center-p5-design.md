# 统一同步中心 P5 收口设计（2026-08-13）

> 状态：用户已确认方案并授权连续实施、PR、部署与生产验证。本文是原总设计第 7、9 节 P5 的细化；如有冲突，以本文和用户本轮明确要求为准。

## 1. 目标

P5 把同步入口收敛到 `/sync/`，并让页面在首次执行前就能看见所有已登记、可自动同步的表格：

- `/sync/` 是唯一同步控制面：四类资产、作业总览筛选、定时配置、全部同步、单文档/单表同步、运行时间线与详情。
- `/exports/` 是纯导出面：继续按 T+ ERP、企微 A、企微 B、飞书分类，只保留下载；移除同步设置、全部同步、单项同步和创建副本。
- `/tplus-sync/` 永久重定向到 `/sync/?group=tplus`。
- doc worker 对 `external_sources` 中当前有效的表级来源做作业目录对账；没有跑过的表也预建 `sync_jobs`，但不创建虚假 `sync_job_runs`。
- P4 的 doc/T+ 调度模式继续保持 `shadow`，P5 不切 `active`，不改变真实调度控制流。

## 2. 已确认事实与边界

### 2.1 当前数据与页面

- `/exports/` 的目录数据已按四类分组并持续产出；P5 不重做导出文件生成逻辑。
- 当前 `sync_jobs` 只在真实同步开始时由 writer upsert，因此“已登记但尚未跑过”的表不会出现在作业总览。
- 文档同步的真实执行单元是表级 `external_sources.id`：企微 `smartsheet_sheet`，飞书 `bitable_table`。整簿手动请求仍通过 doc 级锚点触发重扫，从而发现新增表。
- T+ 的 19 类导出文件是 `chanjet.full` 的产物/步骤，不拆成 19 个虚假作业。

### 2.2 企微 ID 安全与可用性

- `dc...` 是企微 API 使用的文档主键；网页分享地址中的 `s3_...` 不能用于 API，且没有可靠转换接口。
- `source_type='smartsheet_link'` 的历史 `s3_` 行不可自动同步：统一中心可以展示名称和不可用原因，但不得提供同步按钮、不得创建请求。
- 作业、资产和运行 API 不返回 `external_doc_id`；公开仓库的代码、测试、设计、日志样例不得包含真实 docid、真实分享 ID 或群 ID。
- 可同步来源只认显式 allowlist，不能用“非空 external_doc_id”代替来源类型校验。

## 3. 总体架构

```text
/sync/ UI
  ├─ GET /v1/sync/assets                 四类资产与目录覆盖
  ├─ GET/PUT /v1/sync/config/{doc,tplus} 两套真实 legacy 配置面
  ├─ POST /v1/sync/run-all               全部有效文档 + T+ full
  ├─ POST /v1/sync/assets/{id}/run       整簿重扫
  ├─ POST /v1/sync/jobs/{key}/run        单表或 T+ full
  └─ 既有 overview/runs/detail/alerts

backend-api
  ├─ sync_control.py：读资产、校验来源、入队、配置读写
  ├─ sync.py：管理员 API 与错误边界
  └─ 旧 ops/exports 写端点：保留兼容别名，委托同一 service

doc-sync-worker
  └─ SyncJobPlatformWriter.reconcile_document_jobs()
       ├─ active smartsheet_sheet / bitable_table → upsert enabled job
       ├─ 已不在有效集合的 doc pull job → enabled=false
       ├─ 保留历史 runs/steps/alerts
       └─ 不创建 run，不访问外部 API

/exports/ UI
  └─ GET catalog + download only
```

## 4. 作业目录对账

### 4.1 来源 allowlist

表级作业只从以下行生成：

| provider | source_type | 额外条件 |
|---|---|---|
| `wecom` | `smartsheet_sheet` | `status='active'`、doc/sheet ID 均非空 |
| `feishu` | `bitable_table` | `status='active'`、app/table ID 均非空 |

明确排除 `smartsheet_link`、`smartsheet_link_sheet`、结构备份来源、禁用行和 doc 级锚点。

### 4.2 upsert 语义

- `job_key = '<provider>.doc.<source_id>'`，保持 P1 writer 的既有键格式。
- `kind='pull'`、`provider` 与来源一致、`source_id` 指向表级行。
- `display_name` 优先使用“文档名 / 表名”，回退 `source_name`，永不回退到 docid。
- 新行继承当前 doc legacy 调度配置；已有行保留人工设置的 SLA、artifact、alert 和非空 schedule。
- 当前有效集合之外、且属于 doc pull 域的已有作业只设 `enabled=false`，不删除。
- 所有 upsert 显式写 `updated_at=NOW()`。

### 4.3 调用时机与失败策略

- 生产 `run-loop` 启动时先对账一次，使历史已登记表立即进入目录。
- 每次文档全量/整簿重扫结束后再对账，使新增、改名、禁用的表在同轮收敛。
- 对账失败回滚并 fail-open：不得阻断 legacy 同步、手动请求、notifier 或 P4 shadow。
- 注入测试路径不访问数据库，避免改变现有纯 loop 测试行为。

## 5. 统一控制 API

所有端点使用 `require_admin`；所有写请求经 DB 队列，backend 不直接调用企微、飞书或畅捷通 API。

### 5.1 `GET /v1/sync/assets`

返回固定四组：`tplus`、`wecom_company_a`、`wecom_company_b`、`feishu`。

- T+ 项来自既有导出目录，统一关联 `job_key='chanjet.full'`。
- 文档项按 provider/profile/doc 聚合，只返回名称、数值型 anchor `source_id`、表数、已建作业数、最近同步时间、是否可同步和原因。
- 有效企微 doc 锚点只接受 `source_type IN ('smartsheet_doc','registry_doc')` 且 ID 形态为 `dc` 开头、长度达到 API 文档主键下限；飞书只接受 `bitable_app`。
- `smartsheet_link` 作为不可同步项返回 `syncable=false`、`reason='缺少有效企微 docid'`，不返回其 ID 字符串。
- 响应中禁止出现 `external_doc_id`、app token、分享链接或凭据字段。

### 5.2 配置端点

- `GET/PUT /v1/sync/config/doc`
- `GET/PUT /v1/sync/config/tplus`

请求/响应沿用当前 ops 配置结构和 1～168 小时边界。写入必须同时更新 `integration_sync_config` 与对应 `sync_jobs.schedule`，事务失败全部回滚。旧 `/v1/ops/.../sync-config` 作为兼容别名调用同一 service。

### 5.3 触发端点

- `POST /v1/sync/run-all`：为每个有效 doc 锚点创建整簿请求，并请求一次 T+ full。
- `POST /v1/sync/assets/{source_id}/run`：只接受有效 doc 锚点；s3/link/structure/disabled 行返回 400 或 404，不入队。
- `POST /v1/sync/jobs/{job_key}/run`：
  - `chanjet.full` → T+ full queue；
  - enabled 的企微/飞书 pull job → 对其表级 `source_id` 创建手动请求；
  - 其他 job（例如 reconcile）返回 400。
- 文档请求对同一 `source_id` 的 pending/running 行去重；T+ 沿用当前 pending/running 去重。
- 响应给出 queued/skipped 数量与 request ID，不返回外部文档 ID。

旧 `/v1/exports/.../sync-requests`、`/v1/exports/sync-all` 与 `/v1/ops/tplus/full-sync` 暂保留为兼容别名，但新页面不再调用。创建副本后端兼容端点暂不扩展；`/exports/` 不再暴露入口。

## 6. 读模型与筛选

`GET /v1/sync/overview` 在不泄露外部 ID 的前提下补充：

- `source_group`：`tplus` / `wecom_company_a` / `wecom_company_b` / `feishu`；
- `document_name`、`sheet_name`、`env_profile`；
- `enabled` 继续作为目录状态。

作业总览增加以下筛选，行为与时间线筛选一致且可组合：

- 分类：四类；
- 状态：running / success / partial / failed / never；
- 新鲜度：fresh / warning / stale / never / unmonitored；
- 作业：下拉 + 名称搜索。

筛选只影响显示，不重新请求或修改数据。URL `?group=` 预选分类；现有 `?job=` 保持兼容。

## 7. 页面行为

### 7.1 `/sync/`

新增“同步控制”区：

- 顶部“全部同步”按钮；
- 文档同步配置卡与 T+ 配置卡；
- 四类资产标签页：文档显示表/作业覆盖和整簿同步按钮，T+ 显示 19 类产物并以一次 full 为统一触发；不可自动化项只显示原因。

作业表每行保留只读健康信息，并为可触发 job 提供“立即同步”。触发成功后刷新 overview/资产并轮询新 run；连续点击由 loading guard 和后端去重共同保护。

所有新增请求纳入现有 session/load generation：

- 旧响应不得覆盖新筛选、新配置或新资产；
- logout 立即失效所有在途响应并清空管理员数据；
- 失败不提交 pending UI 状态，按钮在 latest request finally 中恢复；
- 所有动态文本经 `esc()`，禁止渲染原始 JSON 或外部 ID。

### 7.2 `/exports/`

- 副标题改为“按来源下载最近一次同步产物”。
- 保留四类 tabs、目录、更新时间、规模和下载。
- 删除同步设置、保存、同步数据列表、立即同步、创建副本及其 JS、定时盲刷。
- 增加“前往统一同步中心”链接。
- catalog API 可以继续提供 `source_id` 兼容字段，页面不读取、不渲染、不发送写请求。

### 7.3 `/tplus-sync/`

public-web nginx 对精确路径 `/tplus-sync/` 返回 301 到 `/sync/?group=tplus`。旧 HTML 可保留一个版本作镜像回滚素材，但正常请求不可再到达。

健康页原 T+ 时间线入口改为统一同步中心的 T+ 预选链接，避免导航仍把旧页当功能入口。

## 8. 数据、事务与竞态

- P5 不新增迁移、不搬业务数据、不删旧 run/request/export 文件。
- catalog reconciliation 与请求入队均显式 commit/rollback；异常后连接必须仍可查询。
- run-all 在一个事务中创建文档请求；T+ 请求通过同一 service 在同一连接内入队，避免前半成功后返回整体失败。若当前代码边界不能共享连接，则响应必须分别报告 doc/T+ 结果，不伪报全成功。
- 同一文档或 T+ 已 pending/running 时返回 skipped/existing，不插重复请求。
- `sync_jobs` 禁用只影响目录与后续平台控制；P4 真实执行仍由 legacy 配置控制，shadow 语义不变。

## 9. 验证

### 9.1 自动化

- backend unit：allowlist、s3 拒绝、无 ID 泄漏、四组资产、单项/全部/T+ 去重、配置事务回滚、HTTPException passthrough。
- writer unit：首次目录 upsert、改名、禁用、schedule 保留、失败 rollback/fail-open、无 run SQL。
- worker unit：仅生产默认装配调用启动/全量后对账；注入 loop 不碰 DB。
- PostgreSQL 16 opt-in integration：真实执行目录对账和请求入队，断言有效来源全覆盖、无 synthetic run、s3/structure 不入队、失败 savepoint 后连接可查、fixture 精确清理。
- frontend strict Node：四类筛选、组合筛选、配置/触发、乱序响应、失败回滚、logout 清空、XSS、exports 无写请求、旧 T+ 入口重定向。
- 全量 root unittest、T+ unittest、navigation、两套 Compose、nginx config、diff/secret scan。

### 9.2 生产验收

部署后逐项保存证据：

1. release `stage-business-cn-peer=success`，业务容器已换到 main 对应镜像；`deploy-business-cn=skipped` 属正常。
2. `/sync/`、`/exports/`、`/health/` 200；`/tplus-sync/` 301 到 T+ 分类。
3. `/exports/` 页面无同步/复制控件，四类下载目录仍可读。
4. 管理 API 的 assets/overview 不包含外部 docid 或分享 ID。
5. 生产触发一次 canonical run-all；有效 doc 锚点和 T+ 各至多一条 pending/running，请求中无 link/structure 来源。
6. 等 worker 消费后，用动态 SQL 比较 eligible 表级 source_id 与 enabled doc `sync_jobs.source_id`，missing=0、extra=0；不得用写死数量代替覆盖判断。
7. 新增 run 全部可在时间线看到；失败项单独列明，不用“部分完成”冒充成功。
8. doc/T+ 容器仍为 `SYNC_SCHEDULER_MODE=shadow`，active 计数为 0；无新增 open alert 或 scheduler traceback。

## 10. 回滚

- 回滚 public-web 镜像即可恢复旧 `/exports/` 与 `/tplus-sync/` 页面。
- 回滚 backend-api 镜像即可撤新 canonical API；兼容旧端点使旧页面仍可工作。
- 回滚 doc-sync-worker 镜像停止目录对账；已建 `sync_jobs` 仅是元数据，可保留，不影响 legacy 同步。
- 不需要数据库 down migration；不删除历史数据。
- 任一回滚都不得把 P4 模式改成 active；生产 SOPS 继续固定 shadow。
