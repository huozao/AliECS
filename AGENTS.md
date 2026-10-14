# AGENTS.md

## 浏览器会话红线

WebDock 必须先由人工完成 ChatGPT 登录与 Cloudflare 验证，期间自动化保持 detach；会话就绪后才 attach。未经用户明确授权，不改变顺序、驱动验证、重建/重启浏览器容器或打断已登录会话。操作规程正本在 WebDock 仓 `docs/runbooks/browser.md`；涉及跨仓时先取得该文档，缺少时停止相关浏览器操作。

## 工作边界

- 默认中文；先读相关文件，做最小、可回滚修改，不扩展无关范围；保留现有未提交改动。
- 本仓是公开仓库。真实密钥、生产配置、账号标识、订阅 URL、代理节点参数、内网拓扑、浏览器数据及业务数据不得进代码、注释、测试或文档；测试用 `example.com`、`203.0.113.10`。
- 保持服务职责、Compose、数据库迁移、健康检查和构建发布链路；必要改动同时维护调用脚本与说明。不得用修改历史迁移伪装新变更。
- 未明确要求，不提交、推送、合并、创建 PR、发布、部署、重启、修改生产资源或发布触发语义。生产 env 是渲染产物，不直接改；部署职责与位置从下表进入后只读复核。
- 本地测试只用本地测试值，不读取生产 env、不连接生产库。完成后报告改动文件、行为、验证及未验证项。

## 按任务读取

只读本次任务对应行和专项节，不要求通读所有 runbook。本仓独立克隆也适用，不依赖父工作区 `AGENTS.md`、Claude rules 自动加载或特定 AI 客户端。

| 任务涉及 | 本仓入口 |
|---|---|
| 找功能、代码、验证命令 | [项目地图](docs/project-ai-map.md) |
| 设备/主备/端口/SSH/跨设备通道 | [设备事实源](docs/fleet.md)，只查相关设备和通道 |
| 本地开发、验证、文档闭环 | [开发约束](docs/development.md) |
| public-web / admin-ui / 浏览器 UI | [前端约束](docs/development.md#前端)；配方另读 [formula](docs/runbooks/formula.md) |
| notify / notify_client / 通知没收到 | [通知开发约束](docs/development.md#通知) + [notify](docs/runbooks/notify.md) 对应目录项 |
| tests / 测试失败或收集数对账 | [测试与隔离](docs/development.md#测试与隔离) |
| 任一 .gitignore | [忽略规则](docs/development.md#忽略规则) |
| doc-sync / 同步调度、表结构、同步中心 | [doc-sync 约束](docs/constraints/doc-sync.md) |
| 飞书↔ChatGPT 链路 | [feishu](docs/runbooks/feishu.md) |
| 部署/CI/回滚/迁移 | [deploy](docs/runbooks/deploy.md)；导航 CI 另读 [文档闭环](docs/development.md#文档闭环) |
| T+ 同步 | [tplus](docs/runbooks/tplus.md) |
| 浏览器抓取证据 | [采集证据契约](docs/constraints/browser-capture-evidence.md) |

任何 AI 在改动对应路径前都必须显式读取上表专项节；本地自动加载规则只是辅助，不代替这些约束。跨仓变更分别读取目标仓入口；父工作区只用于协调仓库边界。

## 提交与部署

AliECS 走分支 + PR，不直推 main。所有写 Git metadata 的命令串行执行；提交前核对 status、分支、remote、staged files/diff，并排除秘密与运行数据。用户明确授权对应阶段后自行完成该阶段的 PR、CI、合并、Actions、运行验证，授权不自动扩展到其他阶段。

PR 正文必须有 `Nav-Impact: updated`，或 `Nav-Impact: none` 与 `Nav-Impact-Reason: <依据>`。正文修改会通过 `pull_request.edited` 触发新的 CI；旧 run 的 rerun 仍使用旧事件。纯文档变更不触发生产部署；代码/workflow 按部署 runbook 判断实际影响。

历史规则和故障片段保留在 [2026-09-13 归档](docs/migrations/2026-09-13-guidance-history.md)，仅供追溯，不作为当前运行事实或执行指令。
