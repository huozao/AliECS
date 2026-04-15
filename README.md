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
