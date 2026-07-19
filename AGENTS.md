# AGENTS.md

## ⛔ 最高优先级红线：ChatGPT「人工登录 → 自动化接管」流程（不得擅自更改）

WebDock 的 ChatGPT 会话遵循固定设计，任何 AI 必须严格遵守：

1. **ChatGPT 登录与 Cloudflare 人机验证，必须由人工在 noVNC 手动完成**；完成前，自动化（Playwright/CDP）**必须处于 detach 状态**（不连接 Chrome）。
2. 原因（2026-05-30 实测）：Playwright 连着 CDP 会泄漏 `Runtime.enable`，Cloudflare 判定为自动化 → 人工点击也无限循环；detach 后人工即可通过。
3. 正确顺序：**人工登录/过验证 → 会话养熟（cf_clearance）→ 自动化再 attach 接管**。
4. AI 未经用户明确同意，**不得**：更改此顺序；让自动化在登录/验证阶段 attach 或驱动浏览器；重建/重启 WebDock 容器或改其浏览器启动/attach 逻辑而打断已养熟的会话；把纯人工登录改成自动登录。
5. 涉及 WebDock 浏览器、ChatGPT 登录态、Cloudflare、attach/detach 的任何改动：**先读本条、先与用户确认、再动手**。

## 项目定位与工作方式

AliECS 是以 AI 客户端协作为主要开发方式的 Docker 化 Web/API 项目。目标不是复杂架构，而是让业务功能、部署链路和维护对人和 AI 都清晰。

修改时必须：先读相关文件 → 判断影响范围 → 小步可回滚修改 → 不顺手重构无关模块 → 完成后说明改了什么/为什么/如何验证/有什么风险。需求不明确时做最小合理判断并说明假设。

架构原则：业务主线显性化，避免过度抽象/封装/配置化；功能入口、主流程、数据来源、关键判断、输出、报错位置应一眼可辨。

## 导航与领域约束（按任务加载，未读不得修改对应模块）

| 任务涉及 | 必读 |
|---|---|
| 设备/主备/端口/SSH | `docs/fleet.md`（单一事实源） |
| 改哪个目录/入口文件 | `docs/project-navigation.md`、`docs/project-ai-map.md` |
| doc-sync-worker / 同步表结构 | `docs/constraints/doc-sync.md` |
| 飞书↔ChatGPT 链路 | `docs/runbooks/feishu.md` |
| 部署/CI/回滚 | `docs/runbooks/deploy.md` |
| T+ 同步 | `docs/runbooks/tplus.md` |

## 关键边界

不得破坏：`public-web` / `admin-ui` / `backend-api` / `postgres` 四服务分工、Docker Compose 本地链路、ECS 生产部署链路、数据库迁移链路、健康检查链路、GitHub Actions 构建发布链路。必须修改时，同步检查相关配置、脚本、文档。

修改原则：保持目录结构与入口文件稳定；新功能放最接近业务入口处；不引入无必要依赖；不提交调试代码、真实密钥、生产配置。

## 数据库与迁移

不直接修改历史迁移文件伪装新变更；新迁移保持可重复执行、失败可定位；涉及数据删除、字段重命名、权限变化时明确写出风险。

## GitHub Actions

以稳定、可读、可排查为目标。未经明确要求，不改变"PR 合并后是否自动部署、手工触发是否必填版本号"等发布触发语义；必须调整时写出"修改前行为 / 修改后行为 / 回退方式"。修改 workflow 时同步检查其调用的脚本、环境变量、镜像命名。

## 环境变量与 ECS 路径

- 新增/修改环境变量必须说明用途并同步示例配置；代码应处理缺失场景。真实生产值不进仓库。
- ECS 默认项目目录 `/root/AliECS`；`release-meta.env` 中各路径与其保持一致；如实际不同必须在变更说明中写明。

## 验证要求

本地验证只用 `local/docker-compose.local.yml` + `local/.env.local`（只含本地测试值），不读取生产 env、不连生产库。优先 `scripts/local-smoke-test.ps1|sh`。

- 通用测试：`python -m unittest discover -s tests`
- shell 脚本：`bash -n deploy/ecs/{deploy,migrate,healthcheck,rollback}.sh`
- Compose：`docker compose -f local/docker-compose.local.yml config > /dev/null`（含真实凭证时不粘贴完整输出）
- 前端（尤其 `services/admin-ui/index.html` 内联脚本）：检查 JS 语法/作用域错误 + 至少一次核心入口 smoke（点击能触发网络请求）。

无法验证时必须写明未验证原因和补救命令。

## 输出要求

完成后说明：改了哪些文件 / 解决什么问题 / 是否影响本地运行、部署、数据库 / 已执行与未执行的验证 / 如何回退。基于实际内容，不写空泛总结。完成修改后不自动打开差异或 PR 预览页面。

提交与部署授权规则见工作区顶层 `AGENTS.md`（全工作区唯一版本）。
