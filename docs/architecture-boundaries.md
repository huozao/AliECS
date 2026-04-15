# 架构边界（AI 协作优先）

## 运行归属
- 宿主机原生：Nginx、Certbot、sing-box、ECS 部署脚本运行入口。
- Compose 托管：public-web、admin-ui、backend-api、postgres。

## 事实来源边界
- GitHub 仓库是源码事实来源。
- GHCR 是镜像事实来源。
- ECS 是运行节点，不是源码事实来源。
- Codex 是协作入口，不是部署入口和事实来源。

## 配置边界与代码边界
- 优先走配置：镜像标签、主机地址、数据库密码、Actions secrets。
- 必须改代码：API 行为、部署控制流、迁移逻辑、Future Sync 处理逻辑。

## 强约束
- 不容器化 Nginx。
- 不容器化 sing-box。
- 数据库迁移必须早于服务切换。
- Actions 负责交付编排，ECS 负责运行时执行。
