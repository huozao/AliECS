# 版本记录

## v2.1.7：新增 mcp-coding-server 编程桥接服务（阶段一只读 PoC）

- 新增 `services/mcp-coding-server`：MCP streamable-http 服务，供 ChatGPT 开发者模式连接器接入；阶段一仅含只读工具 `ping`/`server_info` 与 `/healthz`。
- 接入 CI 依赖安装、release 构建矩阵、`compose.prod.yml`（127.0.0.1:8090）、`deploy.sh` 镜像清单与 `runtime.env.example` 占位。
- 公网暴露由 ECS Nginx 秘密路径 location 承担，属运行时配置，不入库。

## v2.1.6：修复首页登录提交流程并完善 Couple 入口体验

- 修复首页登录弹窗“提交”无效问题。
- 优化 Admin UI 与 Couple 入口展示与跳转逻辑。
- 收敛 PR 辅助工作流触发频率，减少重复检查噪音。


## v2.1.3：当前稳定版本基线

- 记录当前仓库已有的部署、权限、Admin UI、ECS 发布流程状态。

## v2.1.4：完善版本号自动发布流程

- 新增 VERSION 版本号文件。
- 新增 CHANGELOG 中文版本记录。
- GitHub Actions 自动读取 VERSION 作为镜像标签。
- 规范后续提交、PR 标题与版本说明使用中文。