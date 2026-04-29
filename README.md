# AliECS AI 协作友好项目（Phase 1）

## 项目定位
- **GitHub**：源码事实来源（仓库：`https://github.com/huozao/AliECS`）。
- **GHCR**：镜像事实来源。
- **阿里云 ECS**：运行节点。
- **Codex**：协作入口（读仓库、提建议、给补丁），不是事实来源和自动上线入口。

## 已完成范围（Phase 1）
- 本地可运行：`public-web + admin-ui(占位) + backend-api + postgres`。
- API 提供健康检查和就绪检查（含数据库连通探测）。
- CI 做语法与 compose 校验。
- 发布流支持构建/推送三类镜像并通过 SSH 触发 ECS 部署。
- ECS 部署主流程明确：`先迁移 -> 再切换 -> 再健康检查 -> 失败回滚`。
- Future Sync Pipeline 只保留可执行骨架，不实现复杂业务。

## 本地快速启动
```bash
docker compose -f local/docker-compose.local.yml up --build
```

访问地址：
- 公网展示页（本地）: http://localhost:8080
- 管理后台占位页（本地）: http://localhost:8081
- API 健康检查（本地）: http://localhost:8000/healthz


## 自动化部署与日志
- **主路径（推荐）**：`v*` tag 触发 `.github/workflows/release-deploy.yml`，Actions 构建并推送 GHCR 镜像后，SSH 到 ECS 执行 `deploy/ecs/deploy.sh <tag>`。
- **过渡路径（可选）**：ECS 侧 `deploy/ecs/auto-sync.sh` 可做“拉代码 + 重建 + 健康检查”的一次性自动化。
- **镜像命名规范**：
  - `ghcr.io/<owner>/public-web:<tag>`
  - `ghcr.io/<owner>/admin-ui:<tag>`
  - `ghcr.io/<owner>/backend-api:<tag>`
- **日志查看**：
  - Actions 发布日志：GitHub Actions 页面（工作流：`发布并部署`）
  - ECS 发布日志：`/opt/app/deploy/ecs/logs/deploy-YYYYMMDD.log`
  - 容器运行日志：`docker compose --env-file /opt/app/deploy/ecs/runtime.env -f /opt/app/deploy/ecs/compose.prod.yml logs --tail=200`
- **GitHub Secrets（自动部署必需）**：
  - `ECS_HOST` / `ECS_USER` / `ECS_SSH_KEY`
  - 私有 GHCR 推荐再配：`GHCR_USERNAME` / `GHCR_TOKEN`（`read:packages`）
- **触发方式差异**：
  - `git tag vX.Y.Z && git push origin vX.Y.Z`：正式发布（推荐）
  - `workflow_dispatch`：手工重试/补部署（必须输入 `image_tag` 且格式为 `v*`）
- **主页设计依据**：`DESIGN.md`（参考 VoltAgent/awesome-design-md 的 Claude 设计语言，非像素级复制）。
- **详细文档**：
  - 自动部署总说明：`docs/auto-deploy-guide.md`
  - ECS 首次部署清单：`docs/ecs-first-deploy-checklist.md`
  - ECS 运维记录模板：`docs/ecs-operation-record-template.md`

## 入口导航
- 架构边界：`docs/architecture-boundaries.md`
- 修改导航：`docs/project-navigation.md`
- Ubuntu 24.04 新手部署说明：`docs/ubuntu-24.04-ecs-部署指南.md`
- 本地运行入口：`local/docker-compose.local.yml`
- ECS 部署入口：`deploy/ecs/deploy.sh`
- 数据库迁移入口：`deploy/ecs/migrate.sh`
- 健康检查入口：`deploy/ecs/healthcheck.sh`


## 贡献规范
- 贡献流程与中文提交说明：`CONTRIBUTING.md`


## 登录与权限（新）
- `backend-api` 提供 `POST /v1/auth/login`、`GET /v1/auth/me`、`GET /v1/features`。
- 可通过环境变量 `AUTH_USERS_JSON` 配置用户、密码、角色、权限（如 `personal`）。
- `public-web` 首页会根据登录状态控制“个人板块（人体周期）”入口是否可访问。
