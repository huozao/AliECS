# 项目修改导航（给人和 AI）

## 1）主流程
1. 开发与交付：
   本地改代码 -> 本地 compose 验证 -> push GitHub -> Actions 构建/推送 -> SSH 触发 ECS 部署脚本。
2. 运行链路：
   互联网 -> 宿主机 Nginx -> public-web/admin-ui/backend-api -> PostgreSQL。
3. 代理链路：
   代理客户端 -> 宿主机 sing-box -> 外部互联网。
4. Future Sync（骨架）：
   scheduler -> worker -> fetch -> validate -> upsert -> writeback。

## 2）改需求去哪里
- 改公网展示页面：`services/public-web/`
- 改后台占位页面：`services/admin-ui/`
- 改 API 行为和路由：`services/backend-api/app/`
- 改数据库迁移 SQL：`db/migrations/`
- 改本地运行拓扑：`local/docker-compose.local.yml`
- 改 CI/CD 编排：`.github/workflows/`
- 改 ECS 部署流程：`deploy/ecs/`
- 改 Future Sync 骨架：`sync-pipeline/`

## 3）关键入口（必须显式）
- 自动部署总说明：`docs/auto-deploy-guide.md`
- ECS 首次部署清单：`docs/ecs-first-deploy-checklist.md`
- ECS 运维记录模板：`docs/ecs-operation-record-template.md`
- 本地运行入口：`local/docker-compose.local.yml`
- API 主入口：`services/backend-api/app/main.py`
- 迁移入口：`deploy/ecs/migrate.sh`
- 部署入口：`deploy/ecs/deploy.sh`
- 健康检查入口：`deploy/ecs/healthcheck.sh`
- 回滚入口：`deploy/ecs/rollback.sh`
- ECS 自动同步入口：`deploy/ecs/auto-sync.sh`

## 4）不要破坏的边界
- Nginx 和 sing-box 保持宿主机原生，不容器化。
- GitHub Actions 只做交付编排，不做运行时托管。
- 部署顺序固定：先迁移后切换。
- 不要把业务逻辑塞进泛化的 `utils/common/shared`。
