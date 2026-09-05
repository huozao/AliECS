# AliECS AI 项目地图（代码位置版）

> 给 AI 用：功能名 → 代码位置 → 输入/输出 → 验证方式。**本文是 AliECS 内「改哪里」的唯一事实源**——
> 2026-09-01 起 `docs/project-navigation.md` 已并入本文并删除，别再找那份简表。
> 功能的人类语义对照见工作区顶层 `功能地图-人类版.md`。默认只改 AliECS；涉及 webdock/infra 见顶层 AGENTS.md 边界。

## services/backend-api

FastAPI 总后端。`app/main.py` 只做装配，业务在 `app/core.py` + `app/routers/`（拆域见 `docs/backend-api-domains.md`）：
<!-- nav-check: services/backend-api/app/main.py -->
<!-- nav-check-python: services/backend-api/app/main.py:app -->

| router | 功能 |
|---|---|
| `recipes.py` | formula 配方查询、按子件反查、成本核算（坑见 `docs/runbooks/formula.md`） |
| `formula_colors.py` | 标准型号色彩空间只读数据（企微「标准型号0117」+ T+ 当前有效 BOM 父件名称） |
| `exports.py` | 对比表导出（真 xlsx，`compare_export.py`） |
| `tplus_bom.py` | BOM builder（写回 T+ 经独立 write-worker） |
| `auth_admin.py` / `auth_oidc.py` | 本地登录 + SSO(Authelia OIDC) |
| `miniapp_accounts.py` | 微信小程序账号 |
| `webhooks/` | 集成 webhook 网关（chanjet、wecom、飞书） |
| `wecom_assistant.py` | 企微统一助手 |
| `system_config.py` | 同步调度等系统配置（DB 生效面） |
| `ops.py` | /v1/ops/*（T+ timeline、sync-config） |
| `app/routers/sync.py` | `/v1/sync/*` 统一同步中心，查询层为 `app/sync_read.py`，控制层为 `app/sync_control.py`；`app/document_locator.py` 统一资产下载、副本幂等登记与 docid 修复，响应不返回外部文档 ID |
| `versions.py` | 版本看板 |
| `backups.py` | 企微结构备份看板、镜像清理策略看板 |
| `clash_profile.py` | Clash 配置合成器（人类叫法：订阅合并 / 一个订阅选所有节点）。机场订阅源 CRUD + 合成配置下载；渲染逻辑在 `app/clash_profile/render.py`，自建节点走 env `CLASH_SELF_NODES_B64`。`mobile` 目标会把启用订阅源的最新快照嵌成单文件 YAML；2026-09-05 已修复 provider 节点缩进导致的手机 YAML 解析错误。验证：`python -m unittest discover -s tests -p "test_clash_profile_render.py"` |
| `couple.py` | Couple 私密情侣空间：回忆、地图、纪念日、愿望清单，以及按用户 Immich API key 的个人库选片/家庭相册归档；AdventureLog 保持独立入口 |

输入：Postgres、runtime env、T+ worker 只读输出（`/app/tplus-output`）。
验证：`python -m pytest tests`（CI 同一条）；相关单测 patch 目标=函数所在文件（已拆域）。
⚠️ 2026-08-31 起不要再用 `python -m unittest discover -s tests` 当全量判据——它**收不到**
裸函数式用例（`def test_x()` 而非 TestCase 方法），实测漏跑 355 个（分布在 18 个文件里）。

### app/notify/

所有出站通知的唯一出口（飞书 / 企微群机器人 / 企微自建应用）。生产者只产
`Notification`（标题、段落、图），渲染成各家原生格式是 `channels/` 的事。

汇聚点是 `notify_outbox` 表而不是 HTTP：doc-sync-worker 是另一个镜像、构建上下文
互不可见，它只写库（`app/notify_client.py`），投递代码只在 backend-api 这一份。
坑与判据见 `docs/runbooks/notify.md`；飞书那套上传与卡片移植自 `deploy/openclaw-bridge`。
`app/routers/gold_spread_alerts.py` 是黄金价差的业务适配器：校验错单/复盘字段后构造统一
`Notification`，不直接发送飞书；复盘进度使用结构化 `fields` 展示分区分母、运行时间、
预计剩余时间和正式报告指标。重复 `dedup_key` 必须从既有 `notify_deliveries` 回读当前状态；
通用 `GET /v1/internal/notify/deliveries/{outbox_id}` 按来源隔离返回实际投递凭证。
一条路由都没命中的 outbox 行会被写一条墓碑投递记录（`channel='none'`、`status='dead'`），
否则它永远满足「有 outbox 无 deliveries」的孤儿判据、被 flush 无限重领养。
标题图标只在 `Notification.display_title()` 一处决定，飞书卡片头 / 企微 markdown 首行 /
纯文本兜底三处共用；生产者自带图标时不再叠加级别图标。

## services/doc-sync-worker

企微智能表格 + 飞书多维表 → Postgres 的独立 worker。约束必读：`docs/constraints/doc-sync.md`。
输出表：`external_sources/fields/records`、`sync_runs`、`sync_requests`、`sync_jobs/runs/steps`。
统一同步作业双写入口：`app/storage/sync_job_platform.py`。
`app/pipelines/sync_alert_notifier.py` 是 P3 告警事实源：轮询 `sync_jobs` 与 `sync_job_runs`，以
`sync_job_alerts` 的 partial unique open claim 去重；行锁只保护投递和恢复，resolved 后由 partial unique 允许重开，步骤清理由独立 DELETE 按 30/90 天执行。
调度候选内核在 `app/pipelines/sync_scheduler.py`；`sync_jobs.schedule` 是候选配置面，legacy `integration_sync_config` 仍驱动 shadow 期的真实执行并作为回滚面。shadow 证据只合并到已有真实 `trigger='schedule'` run 的 `detail_json.shadow`，不创建伪 run。
文档定位档案在 `document_locator_registry/events/mirror_jobs/copy_requests`；导入为 `app/pipelines/document_locator_import.py`，源同步后对账为 `document_locator.py`，企微人工镜像为 `document_locator_mirror.py`，它在备份文档里维护三张表：「文档定位档案」（文档级）、「定位档案变更历史」、「同步表格清单」（表级身份，含子表 ID 与来源 ID，随全量同步整表刷新）；三张表都写完即回读校验，写不进就失败重试。真实 docid、分享标识、管理员与凭据引用只落生产私有库/企微镜像，不进公开仓。
验证：`SYNC_ALERT_INTEGRATION_DATABASE_URL=<postgres-url> python -m unittest discover -s tests -p "test_sync_alert_notifier_integration.py" -v`。

## services/tplus-sync-worker

畅捷通 T+ 只读拉取（BOM/存货/价格）。排障：`docs/runbooks/tplus.md`。
统一同步作业双写入口：`src/tplus_datahub/jobs/sync_job_platform.py`。
调度候选内核在 `src/tplus_datahub/jobs/sync_scheduler.py`，与 doc-sync 副本字节相同；候选配置、legacy 回滚和真实 scheduled run 上的 shadow 证据语义与上节一致。
测试要 `PYTHONPATH="src;."`；根 CI 同时运行子项目 unittest。

## services/public-web

公网首页（纯 nginx 静态）：功能卡片、登录、formula 入口、工具分区（灰分计算器）。
`services/public-web/sync/index.html` 对应 `/sync/` 管理员统一同步中心，按 T+ ERP、企微 A、企微 B、飞书与「系统任务」分类展示资产，统一提供下载、复制、docid 修复、调度、立即运行、时间线、步骤详情与告警。2026-08-20 起**按文档展示**（同步粒度本就是整簿），原「作业总览」区块已并入「同步资产」：表级作业按 `doc_source_id` 聚合到文档行，表级明细只在该文档有 failed/partial 或未解决告警时自动展开；不挂任何文档的作业进「系统任务」。判据见 `docs/constraints/doc-sync.md`。`/exports/` 相对 301 到 `/sync/?view=assets`，`/tplus-sync/` 相对 301 到 `/sync/?group=tplus`。
`formula/index.html` 是系统配方页（查询 → 版本对比 → 成本核算），纯前端渲染 + `compare-core.js`（对比矩阵/行序/列序/视图开关）
+ `cost-core.js`（利润口径）。「查询方式」可切**按配方**或**按子件反查**（两段式：候选罗列 → 勾选确认 → 反查）。
坑与判据全在 `docs/runbooks/formula.md`，改这三个文件之前先读；接口契约见 `docs/recipe-query.md`。
⚠️ `compare-core.js` 是微信小程序共享模块的权威源，改完要在 weapp 仓跑 `sync-shared.mjs`。
`formula/colors/` 是标准型号色彩空间（three.js + camera-controls，数据走 `/v1/formula/colors`，
需登录且有 `formula.read`）；`mock-data.js` 是默认隐藏的参考示例，惰性加载。视图设置已从画布浮层移到顶部 `#settingsPanel`；色点标签由 `rebuildLabels()` / `syncLabels()` 的 DOM 层渲染，偏好存 `localStorage['aliecs_formula_colors_view_prefs']`。
「刷新数据」按钮走 `POST /v1/formula/colors/refresh` 入队 `sync_requests` 后轮询 `meta.last_sync_at`（死线 180s）——
它**只重拉企微表**，页面上的父件名称/匹配状态来自 T+ 两张表，不在这条链路上。页内所有请求都经 `api(path, options)`，
第二个参数会展开进 `fetch` 的 init；漏掉它会把 POST 悄悄降级成 GET。
生产热更新可 `docker cp`；HTML 已加 no-cache 头。验证：JS 语法检查 + 浏览器 smoke。

⚠️ `exports/` 与 `sync/` 引用共享资产 `common/admin.css` + `common/admin-auth.js`；旧 `tplus-sync/` 仅保留重定向兜底页
（后者导出 `window.AliECSAdmin`，页面内联脚本第一条语句就解构它）。**热更新必须成对拷贝**：
只 `docker cp` 一个 `index.html` 而不带 `common/`，会让内联脚本在第一行抛 `ReferenceError` 中止，
整页 onclick 全不绑定、登录闸门失效。镜像整体部署是原子的，只有 `docker cp` 这条路径有此风险。

## services/admin-ui

管理后台（`index.html` 内联脚本）。改后必须做前端可执行性验证（语法/作用域错误 + 核心入口点击触发网络请求）。

## services/coding-executor / services/mcp-coding-server

MCP 编程路线（OAuth 已上线；⚠️ ECS nginx 域根的 OAuth 路由不在 git）。

## deploy/openclaw-bridge

飞书 ↔ OpenClaw 的 bridge（`openclaw_bridge.py`）。当前运行位置只查 `docs/fleet.md` 并实测；排障见 `docs/runbooks/feishu.md`。
单测 `tests/test_openclaw_bridge.py`；合入 main 后仅在 bridge 内容树变化时自动 cutover，回滚、重切和非 main ref 仍走手工 `workflow_dispatch`。

## deploy/ecs

生产 compose 与部署脚本。当前 business-cn 运行在 txecs：
源码 `/srv/business-cn/current`，Compose env `/srv/business-cn/config/compose.env`；
排障见 `docs/runbooks/deploy.md`。运行 env 是渲染产物
（真源=infra `secrets/txecs-production.enc.env`），勿在主机长期直改。
验证：`docker compose --env-file deploy/ecs/runtime.env.example -f deploy/ecs/compose.prod.yml config`。

入口脚本与工作流：

| 动作 | 入口 |
|---|---|
| 生产角色部署 | `deploy/ecs/deploy-role.sh` |
| 数据迁移 | `deploy/ecs/migrate.sh` |
| 健康检查 | `deploy/ecs/healthcheck.sh` |
| 回滚 | `deploy/ecs/rollback.sh`、`deploy/ecs/emergency-rollback.sh` |
| 构建与目标选择 | `.github/workflows/release-deploy.yml` |
| bridge 切换 | `.github/workflows/bridge-cutover.yml` |

语法检查：`bash -n deploy/ecs/{deploy-role,migrate,healthcheck,rollback}.sh`。

## db/migrations

迁移 SQL。写成幂等；生产 psql =
`ssh txecs 'sudo docker exec -i business-cn-postgres-1 psql -U app -d app'`。

## local

本地验证专用：`docker-compose.local.yml` + `.env.local`（只放本地测试值）。smoke：`scripts/local-smoke-test.ps1|sh`。

## 各目录禁提交

真实 env/token/密钥、生成的 Excel/下载物、logs、业务数据导出、含凭证截图。
