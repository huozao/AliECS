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
| 设备之间传文件/镜像、跨境推 TCR 失败 | `docs/fleet.md`「设备间既存通道」+ `docs/runbooks/deploy.md`「应急旁路」。已列出可复用通道、各主体拉 GHCR 的实测速率、以及**确认不通的连接**，不要重新逐条试探 |
| T+ 同步 | `docs/runbooks/tplus.md` |

## 关键边界

不得破坏：`public-web` / `admin-ui` / `backend-api` / `postgres` 四服务分工、Docker Compose 本地链路、ECS 生产部署链路、数据库迁移链路、健康检查链路、GitHub Actions 构建发布链路。必须修改时，同步检查相关配置、脚本、文档。

修改原则：保持目录结构与入口文件稳定；新功能放最接近业务入口处；不引入无必要依赖；不提交调试代码、真实密钥、生产配置。

**本仓是 PUBLIC 仓库**（`gh repo view huozao/AliECS` 可核实；工作区顶层表格只给 `infra` 和 `material_rnd` 标了私有，容易误判成本仓也是私有）。提交任何内容前先确认它可以公开：代理节点的地址/端口/传输与伪装参数/凭据、第三方服务的订阅 URL 与账号标识、内网拓扑细节，一律走环境变量（SOPS 渲染）或数据库，不进仓库、不进注释、不进测试 fixture、不进设计文档。测试一律用 `example.com`、`203.0.113.10`（RFC 5737）这类占位值。

## 数据库与迁移

不直接修改历史迁移文件伪装新变更；新迁移保持可重复执行、失败可定位；涉及数据删除、字段重命名、权限变化时明确写出风险。

## GitHub Actions

以稳定、可读、可排查为目标。未经明确要求，不改变"PR 合并后是否自动部署、手工触发是否必填版本号"等发布触发语义；必须调整时写出"修改前行为 / 修改后行为 / 回退方式"。修改 workflow 时同步检查其调用的脚本、环境变量、镜像命名。

## 环境变量与部署路径

- 新增/修改环境变量必须说明用途并同步示例配置；代码应处理缺失场景。真实生产值不进仓库。
- 当前设备职责和部署路径只从 `docs/fleet.md` 与 `docs/runbooks/deploy.md` 进入，并在目标设备只读复核；不得把历史 `/root/AliECS` 当作所有角色的默认路径。

## 文档闭环

代码或配置验证通过后必须回查路径、符号、配置键、API、部署、回滚、备份和专项 runbook。PR 必须记录 `Nav-Impact: updated`，或同时记录 `Nav-Impact: none` 与 `Nav-Impact-Reason: <依据>`；最后从工作区顶层 `AGENTS.md` 重新进入并复验一条“功能 → 代码 → 验证命令 → 运行位置”链路。

**⚠️ 这条记录必须写在 PR 正文里，写在 commit message 里不算。** `ci.yml` 的「PR 导航影响记录」步骤读的是 `github.event.pull_request.body`，本地 `check_nav_impact.py --range` 过了不代表 CI 会过（2026-08-16 PR#320 就是这样红的）。两处都写最稳。

补救时注意：`ci.yml` 只在 `opened / synchronize / reopened / ready_for_review` 触发，**`edited` 不在列**，所以改完正文不会自动重跑；`gh run rerun` 也没用——它重放的是旧 event payload，读到的还是旧正文。要么再推一个 commit（`synchronize`），要么 `gh pr close && gh pr reopen`。

### 断言判据

治理文档正文中以反引号点名的、指向本仓 Python 定义的、非下划线开头的标识符，必须有对应断言：

```
<!-- nav-check-python: 相对路径.py:符号名 -->
```

标记放在点名它的那份文档里，**不写进 `.navigation-check.json`**——断言与它保护的句子脱钩后，CI 报红也定位不到该改哪句（原先 `main.py:app` 两处各写一份，已收敛为只留 inline）。

判据只认“文档点了名”，不认“符号重要”：nav-check 防的是文档漂移，不是重构。文档没提的符号改名不关文档的事，不该红；文档点名的符号一改名，那句话立刻就错，必须红。

三类**不要**断言：

- 函数内的局部变量。校验器只认模块级与类级定义，断言局部变量会判红（这是刻意的）。
- 文档里其实指数据库表名、JSON 字段、API 参数或环境变量，只是碰巧与某个无关文件里的模块级名字重名。例如 `sync_runs`、`sync_job_runs` 是表名，不是 Python 符号。
- `docs/plans/`、`docs/migrations/` 等历史材料点名的符号。它们是时间点证据不是当前事实源，把治理耦合到归档材料上是反向依赖。

判据示例本身必须写在代码围栏里——校验器只剥围栏，行内反引号挡不住标记正则，写在正文会被当成真断言。

## 验证要求

本地验证只用 `local/docker-compose.local.yml` + `local/.env.local`（只含本地测试值），不读取生产 env、不连生产库。优先 `scripts/local-smoke-test.ps1|sh`。

- 通用测试：`python -m unittest discover -s tests`
- shell 脚本：`bash -n deploy/ecs/{deploy,migrate,healthcheck,rollback}.sh`
- Compose：`docker compose -f local/docker-compose.local.yml config > /dev/null`（含真实凭证时不粘贴完整输出）
- 前端（尤其 `services/admin-ui/index.html` 内联脚本）：检查 JS 语法/作用域错误 + 至少一次核心入口 smoke（点击能触发网络请求）。

无法验证时必须写明未验证原因和补救命令。

## 输出要求

完成后说明：改了哪些文件 / 解决什么问题 / 是否影响本地运行、部署、数据库 / 已执行与未执行的验证 / 如何回退。基于实际内容，不写空泛总结。完成修改后不自动打开差异或 PR 预览页面。

## 提交与部署

- AliECS 变更走分支 + PR，不直推 main。所有写 `.git` 的命令串行执行。
- 用户明确授权提交/推送/部署后，完成 PR、CI、合并、Actions 和运行验证，不停在手工指令。
- 提交前检查 status/分支/remote，并排除 `.env`、logs、browser_data、`_references`、真实密钥和生产数据。
- 纯文档变更不触发生产 deploy；代码/workflow 变更按 `docs/runbooks/deploy.md` 闭环。
