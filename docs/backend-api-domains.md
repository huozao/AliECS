# backend-api 业务域 → 文件 路由表

2026-07-03 将 `services/backend-api/app/main.py`（约 5000 行单体）按业务域粗拆。`main.py` 只保留 FastAPI 装配（app 创建、CORS、请求日志中间件、挂载路由）。找代码先按业务词 grep 各文件头部的中文 docstring。

| 业务域 | 文件 | 主要路由/内容 |
|---|---|---|
| 装配 | `app/main.py` | app 创建、CORS、请求日志中间件、include_router |
| 共享基础设施 | `app/core.py` | 数据库连接 `_conn`、令牌签发/校验、审计 `_audit`、`require_login/require_admin/require_permission`、`DEFAULT_FEATURES` |
| 认证与用户管理 | `app/routers/auth_admin.py` | `/v1/auth/*`、`/v1/features`、`/v1/admin/`（用户/角色/权限/功能/联系人/审计日志/rbac-overview） |
| 健康与运维 | `app/routers/ops.py` | `/healthz` `/readyz` `/v1/ping`、`/v1/ops/*`（T+同步运行/时间线/对账复核/主机状态/微信登录二维码）、企微B消息采集 |
| 配方 | `app/routers/recipes.py` | `/v1/recipes/*`（查询/成本核算/导出/BOM同步/下载）、缓存预热 |
| 数据导出与同步 | `app/routers/exports.py` | `/v1/exports/*`、`/v1/inventory/current-stock`、`/v1/routing/*`、`/v1/admin/doc-sync/*`（企微文档同步） |
| Couple 私密空间 | `app/routers/couple.py` | `/v1/memories` `/v1/photos` `/v1/couple` `/v1/anniversaries` `/v1/bucket-items` `/v1/share`、Immich、照片存储、`/uploads/{name}`、couple 空间管理 |
| 集成 webhook | `app/routers/webhooks/`（原有） | 企微/飞书/畅捷通回调 |

约定：路由函数与它 patch 的辅助函数（如 `_conn`）在同一文件的模块全局里，单测 monkeypatch 时以路由所在文件为 patch 目标。
