项目概述

AliECS 是一个多服务 Web/API 项目（public‑web、admin‑ui、backend‑api），使用 Docker Compose 部署在阿里云 ECS。仓库提供详细文档和部署脚本，本文件只提供最小必要的指导，避免重复已有信息。

核心原则
保护关键服务：修改代码或工作流时，不得破坏三大服务及其部署链路，包括 deploy/ecs 下的部署、迁移、健康检查和回滚脚本。
简化发布：发布由 GitHub Actions 执行，用户只需提供新版本号。工作流负责校验版本、创建 tag、构建镜像并部署。版本号仅标记一次发布，如部署失败视为废版，不得复用；禁止覆盖或删除已有 tag。
清晰日志：工作流应输出中文日志和 Step Summary，列出发布前版本、计划发布版本、部署结果和镜像信息，便于排查。
安全回滚：deploy/ecs/deploy.sh 会记录当前部署版本到 release-meta.env。需要回滚时，应使用 deploy/ecs/rollback.sh 按实际运行版本回滚，而非简单依赖 Git 历史。
最小发布流程
触发：合并代码到 main 后，由用户创建新版本标签（例如 v0.1.5），或在 Actions 页面输入计划发布版本。
执行：release-deploy.yml 校验版本格式、检查重复 tag、创建新 tag、构建 public-web/admin-ui/backend-api 镜像并推送 GHCR，然后通过 SSH 调用 deploy/ecs/deploy.sh <tag> 完成部署和健康检查。
完成：部署成功后，工作流更新 release-meta.env 记录当前版本，并在 Step Summary 中展示发布摘要。若部署失败且 tag 已创建，下次发布请使用下一个版本号。
建议
本文件应保持简短，只在实际遇到问题时增加具体规则（例如指定包管理器、测试命令或格式化脚本）。每条规则需源于实际经验，保持简洁明确。
避免记录代码仓库中已有的信息（如目录结构、语言框架或常见命令），以节省指令预算并减少干扰。
更详细的部署与运维指导见 docs/auto-deploy-guide.md 以及各脚本的内联注释。
