# ECS 服务器操作记录模板

> 用途：记录每台 ECS 的部署信息，便于长期维护与迁移。

## 1. 基础信息
- 服务器名称：
- 服务器公网 IP：
- 地域 / 可用区：
- 系统版本（`cat /etc/os-release`）：
- SSH 用户：
- 首次部署日期：

## 2. 软件版本
- Docker 版本（`docker --version`）：
- Compose 版本（`docker compose version`）：
- Nginx 版本（`nginx -v`）：

## 3. 项目路径与运行路径
- 仓库目录（固定建议：`/opt/app`）：
- 运行 compose 文件路径：
- `release-meta.env` 路径：
- `runtime.env` 路径：
- Nginx 配置文件路径（固定建议：`/etc/nginx/conf.d/aliecs.conf`）：
- 部署日志目录：

## 4. GitHub 与自动部署
- 仓库地址：
- GitHub Secrets 是否配置完成（是/否）：
  - `ECS_HOST`：
  - `ECS_USER`：
  - `ECS_SSH_KEY`：
- 自动部署触发规则（如 `v*` tag）：
- 是否启用 `workflow_dispatch`：

## 5. 当前发布状态
- 当前部署 tag：
- 当前部署 commit：
- 最近一次成功部署时间：
- 最近一次回滚时间：
- 最近一次失败原因摘要：

## 6. 网络与安全
- 域名：
- 是否已启用 HTTPS（是/否）：
- 证书来源（如 Let's Encrypt）：
- 安全组开放端口：
- 数据库端口是否对外开放（必须记录“是/否”）：

## 7. 运行检查
- `./deploy/ecs/healthcheck.sh` 最近结果：
- `curl http://127.0.0.1:8000/readyz` 最近结果：
- `docker compose ... ps` 最近结果：

## 8. 变更记录（持续追加）
| 时间 | 操作人 | 类型（部署/回滚/配置变更） | 内容 | 结果 |
|---|---|---|---|---|
|  |  |  |  |  |

## 9. 备注
- 
