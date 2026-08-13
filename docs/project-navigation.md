# 项目修改导航（给人和 AI）

> 本文只回答“改哪里、先读什么、怎么验证”。当前设备、IP、端口、主备和运行职责统一查 [`fleet.md`](fleet.md) 并只读复核。

## 功能入口

| 要修改 | 代码入口 | 必读/最小验证 |
|---|---|---|
| 公网首页和静态工具 | `services/public-web/` | 服务 README、浏览器 smoke |
| 管理后台 | `services/admin-ui/` | JS 语法 + 核心入口 smoke |
| API、配方、导出、BOM | `services/backend-api/app/` | [`backend-api-domains.md`](backend-api-domains.md)、相关单测 |
| T+ 拉取与写回 | `services/tplus-sync-worker/` | [`runbooks/tplus.md`](runbooks/tplus.md) |
| 统一同步控制台（企微/飞书/T+） | `services/public-web/sync/`、`services/backend-api/app/routers/sync.py` | [`project-ai-map.md`](project-ai-map.md) |
| 数据导出下载 | `services/public-web/exports/`、`services/backend-api/app/routers/exports.py` | [`project-ai-map.md`](project-ai-map.md) |
| 企微/飞书文档同步 | `services/doc-sync-worker/` | [`constraints/doc-sync.md`](constraints/doc-sync.md) |
| 飞书 bridge | `deploy/openclaw-bridge/` | [`runbooks/feishu.md`](runbooks/feishu.md) |
| Clash 配置合成（订阅合并） | `services/backend-api/app/clash_profile/`、`app/routers/clash_profile.py`、admin-ui `sec-clash-profile` | `python -m unittest discover -s tests -p "test_clash_profile_render.py"`，产物再过一遍 `clash-meta -t -f`。⚠️ `-t` 只做静态校验，**测不出机场订阅拉不拉得到**（首版据此误判为通过，实跑节点数 0），改机场相关逻辑必须真跑一次实例查 `/providers/proxies`，见设计文档「手工验证」 |
| 数据库结构 | `db/migrations/` | [`../db/README.md`](../db/README.md) |
| 构建、部署与回滚 | `.github/workflows/`、`deploy/ecs/` | [`runbooks/deploy.md`](runbooks/deploy.md)、[`../deploy/README.md`](../deploy/README.md) |
| 本地拓扑 | `local/docker-compose.local.yml` | `docker compose ... config` |

更细的函数和数据流定位见 [`project-ai-map.md`](project-ai-map.md)。

## 关键入口

- API 装配：`services/backend-api/app/main.py`
<!-- nav-check: services/backend-api/app/main.py -->
<!-- nav-check-python: services/backend-api/app/main.py:app -->
- 生产角色部署：`deploy/ecs/deploy-role.sh`
- 数据迁移：`deploy/ecs/migrate.sh`
- 健康检查：`deploy/ecs/healthcheck.sh`
- 回滚：`deploy/ecs/rollback.sh`、`deploy/ecs/emergency-rollback.sh`
- 构建与目标选择：`.github/workflows/release-deploy.yml`
- bridge 切换：`.github/workflows/bridge-cutover.yml`

## 完成闭环

1. 运行最小代码检查和目标功能验证。
2. 获授权时才做生产更新，并保存 SHA、digest 和健康证据。
3. 回查路径、符号、配置键、API、部署、回滚、备份和 runbook 是否受影响。
4. 更新导航，或记录 `Nav-Impact: none` 与理由。
5. 运行导航检查；从工作区顶层 `AGENTS.md` 重新进入复验一次。
