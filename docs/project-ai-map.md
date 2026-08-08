# AliECS AI 项目地图（代码位置版）

> 给 AI 用：功能名 → 代码位置 → 输入/输出 → 验证方式。功能的人类语义对照见工作区顶层 `功能地图-人类版.md`。默认只改 AliECS；涉及 webdock/infra 见顶层 AGENTS.md 边界。

## services/backend-api

FastAPI 总后端。`app/main.py` 只做装配，业务在 `app/core.py` + `app/routers/`（拆域见 `docs/backend-api-domains.md`）：

| router | 功能 |
|---|---|
| `recipes.py` | formula 配方查询、成本核算 |
| `formula_colors.py` | 标准型号色彩空间只读数据（企微「标准型号0117」+ T+ 当前有效 BOM 父件名称） |
| `exports.py` | 对比表导出（真 xlsx，`compare_export.py`） |
| `tplus_bom.py` | BOM builder（写回 T+ 经独立 write-worker） |
| `auth_admin.py` / `auth_oidc.py` | 本地登录 + SSO(Authelia OIDC) |
| `miniapp_accounts.py` | 微信小程序账号 |
| `webhooks/` | 集成 webhook 网关（chanjet、wecom、飞书） |
| `wecom_assistant.py` | 企微统一助手 |
| `system_config.py` | 同步调度等系统配置（DB 生效面） |
| `ops.py` | /v1/ops/*（T+ timeline、sync-config） |
| `versions.py` | 版本看板 |
| `backups.py` | 企微结构备份看板、镜像清理策略看板 |
| `couple.py` | Couple（相册已由 Immich/AdventureLog 接管，此处仅存量） |

输入：Postgres、runtime env、T+ worker 只读输出（`/app/tplus-output`）。
验证：`python -m unittest discover -s tests`；相关单测 patch 目标=函数所在文件（已拆域）。

## services/doc-sync-worker

企微智能表格 + 飞书多维表 → Postgres 的独立 worker。约束必读：`docs/constraints/doc-sync.md`。
输出表：`external_sources/fields/records`、`sync_runs`、`sync_requests`。

## services/tplus-sync-worker

畅捷通 T+ 只读拉取（BOM/存货/价格）。排障：`docs/runbooks/tplus.md`。
测试要 `PYTHONPATH="src;."` 且 CI 不覆盖，改动必须本地跑。

## services/public-web

公网首页（纯 nginx 静态）：功能卡片、登录、formula 入口、工具分区（灰分计算器）。
`formula/colors/` 是标准型号色彩空间（three.js + camera-controls，数据走 `/v1/formula/colors`，
需登录且有 `formula.read`）；`mock-data.js` 是默认隐藏的参考示例，惰性加载。视图设置已从画布浮层移到顶部 `#settingsPanel`；色点标签由 `rebuildLabels()` / `syncLabels()` 的 DOM 层渲染，偏好存 `localStorage['aliecs_formula_colors_view_prefs']`。
「刷新数据」按钮走 `POST /v1/formula/colors/refresh` 入队 `sync_requests` 后轮询 `meta.last_sync_at`（死线 180s）——
它**只重拉企微表**，页面上的父件名称/匹配状态来自 T+ 两张表，不在这条链路上。页内所有请求都经 `api(path, options)`，
第二个参数会展开进 `fetch` 的 init；漏掉它会把 POST 悄悄降级成 GET。
生产热更新可 `docker cp`；HTML 已加 no-cache 头。验证：JS 语法检查 + 浏览器 smoke。

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

## db/migrations

迁移 SQL。写成幂等；生产 psql =
`ssh txecs 'sudo docker exec -i business-cn-postgres-1 psql -U app -d app'`。

## local

本地验证专用：`docker-compose.local.yml` + `.env.local`（只放本地测试值）。smoke：`scripts/local-smoke-test.ps1|sh`。

## 各目录禁提交

真实 env/token/密钥、生成的 Excel/下载物、logs、业务数据导出、含凭证截图。
