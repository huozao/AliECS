# 2026-09-13 指导文档历史归档

本文保存本次分层治理移出的原文，仅供追溯；不是现行约束、部署指令或当前运行事实。文中当时的版本、账号标识、未完成项和生产状态不能直接用于操作。当前入口是 [AGENTS](../../AGENTS.md)、[开发约束](../development.md)、[项目地图](../project-ai-map.md) 和 [notify](../runbooks/notify.md)。原文中已有标识保留为历史记录，不代表允许新增敏感数据。

## 原 AGENTS.md 全文

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
| 改哪个目录/入口文件 | `docs/project-ai-map.md`（2026-09-01 起是唯一一份，`project-navigation.md` 已并入并删除） |
| doc-sync-worker / 同步表结构 | `docs/constraints/doc-sync.md` |
| 飞书↔ChatGPT 链路 | `docs/runbooks/feishu.md` |
| 部署/CI/回滚 | `docs/runbooks/deploy.md` |
| 设备之间传文件/镜像、跨境推 TCR 失败 | `docs/fleet.md`「设备间既存通道」+ `docs/runbooks/deploy.md`「应急旁路」。已列出可复用通道、各主体拉 GHCR 的实测速率、以及**确认不通的连接**，不要重新逐条试探 |
| T+ 同步 | `docs/runbooks/tplus.md` |
| 出站通知（飞书/企微）、某个告警没收到 | `docs/runbooks/notify.md`。四处旧的各写各的飞书发送已收敛到 `app/notify/`；**接口返回 200 不等于对方收到**，判据看 `notify_deliveries` 不看 `notify_outbox` |

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

代码或配置验证通过后必须回查路径、符号、配置键、API、部署、回滚、备份和专项 runbook。PR 必须记录 `Nav-Impact: updated`，或同时记录 `Nav-Impact: none` 与 `Nav-Impact-Reason: <依据>`——⚠️ **判据落在 PR body 上，不是 commit message**（`ci.yml` 的「PR 导航影响记录」读的是`github.event.pull_request.body`）。commit 里带了、body 里没带，CI 一样红。而且**补进 body 之后 rerun 是没用的**：rerun 重放的是原事件载荷，里面还是旧 body；要么建 PR 前就写好，要么补完再推一个 commit 触发 `synchronize`（2026-09-09 实测）；最后从工作区顶层 `AGENTS.md` 重新进入并复验一条“功能 → 代码 → 验证命令 → 运行位置”链路。

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

- 通用测试：`python -m pytest tests`（CI 用的同一条）。
  ⚠️ **2026-08-31 起不要再用 `python -m unittest discover -s tests` 当全量判据**：
  它只收 `TestCase` 子类，`def test_x()` 这种裸函数式用例一个都不收。实测 discover
  跑 1051 个全绿、pytest 收 1412 个其中 40 个失败——差的 **355 个分布在 18 个文件**里
  从来没被跑过（含 `test_backend_gold_spread_alerts.py` 全部 20 个）。两个 runner 的
  收集数对不上，就说明有测试没在跑。
- **新增 `tests/` 文件时必须遵守既有的 `app` 包隔离协议**（仓里 42 个模块在用）：
  `services/` 下有四个包都叫 `app`（backend-api / coding-executor / doc-sync-worker /
  mcp-coding-server），谁先被 import 谁就占住 `sys.modules['app']`，之后模块里延迟到
  测试体内的 `from app.x import y` 就会拿到**错的包**，报
  `cannot import name 'app' from 'app.main' (…/doc-sync-worker/app/main.py)`。写法：

  ```python
  @classmethod
  def setUpClass(cls):
      for name in list(sys.modules):
          if name == "app" or name.startswith("app."):
              del sys.modules[name]
      sys.path.insert(0, str(BACKEND_ROOT))

  @classmethod
  def tearDownClass(cls):
      ...同样先 purge，再把 BACKEND_ROOT 从 sys.path 摘掉
  ```

  裸函数式文件没有这两个钩子，把隔离放进 `_module()` 那个 helper（见
  `tests/test_backend_wecom_kf.py::_ensure_backend_app`），且**只在占位的不是本 service
  时才清**——无条件清会让每次调用都重新 import、模块身份漂移，`patch` 会打空。
- **测试里改 `os.environ` 必须还原**。`os.environ.setdefault(...)` 裸写在模块级或 helper
  里会永久污染进程，后面的用例走进本不该走的分支（2026-08-31 实测：
  `test_backend_wecom_assistant` 漏还原 `DATABASE_URL`，导致 `test_backend_wecom_kf`
  两个用例去 `mkdir /app/wecom-kf-materials` 而失败，且**单独跑那个文件不复现**）。
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

## 项目地图移出原文

> 给 AI 用：功能名 → 代码位置 → 输入/输出 → 验证方式。**本文是 AliECS 内「改哪里」的唯一事实源**——
> 2026-09-01 起 `docs/project-navigation.md` 已并入本文并删除，别再找那份简表。
> 功能的人类语义对照见工作区顶层 `功能地图-人类版.md`。默认只改 AliECS；涉及 webdock/infra 见顶层 AGENTS.md 边界。

| `clash_profile.py` | Clash 配置合成器（人类叫法：订阅合并 / 一个订阅选所有节点）。机场订阅源 CRUD + 合成配置下载；渲染逻辑在 `app/clash_profile/render.py`，自建节点走 env `CLASH_SELF_NODES_B64`。`mobile` 目标会把启用订阅源的最新快照嵌成单文件 YAML；2026-09-05 已修复 provider 节点缩进导致的手机 YAML 解析错误。2026-09-08 加 `Codex-Win`/`Codex-WSL` 两个组与 `codex-in` listener（7899，仅 desktop），让两处 codex 各自选节点，接线与判据见 `infra/roles/devbox/clash/README.md`〈三点七〉。验证：`python -m unittest discover -s tests -p "test_clash_profile_render.py"` |

⚠️ 2026-08-31 起不要再用 `python -m unittest discover -s tests` 当全量判据——它**收不到**
裸函数式用例（`def test_x()` 而非 TestCase 方法），实测漏跑 355 个（分布在 18 个文件里）。

测试要 `PYTHONPATH="src;."`；根 CI 同时运行子项目 unittest。

验收、生产版本 SHA-256、负载证据和剩余浏览器验收见 `docs/superpowers/plans/2026-09-10-market-three-page-acceptance.md`。

MCP 编程路线（OAuth 已上线；⚠️ ECS nginx 域根的 OAuth 路由不在 git）。

生产 compose 与部署脚本。当前 business-cn 运行在 txecs：
源码 `/srv/business-cn/current`，Compose env `/srv/business-cn/config/compose.env`；
排障见 `docs/runbooks/deploy.md`。运行 env 是渲染产物
（真源=infra `secrets/txecs-production.enc.env`），勿在主机长期直改。

将市场观察当前投影改为每个包内键只写一次，保留原始观察历史。生产 psql =
`ssh txecs 'sudo docker exec -i business-cn-postgres-1 psql -U app -d app'`。

## notify 移出原文

## 为什么不是别的方案

排除掉的三条路，理由留在这里免得再议一次：

- **SSH 统一转发到某台机器**：SSH 是运维通道不是应用通道。没有重试/幂等语义，
  返回码只说明 shell 退出了、不说明对方收没收到，日志散在各机。webdock2 还有
  跨 shell 引号坑（见 AGENTS.md），JSON 写进去会静默变空值。而且 aliecs 停机、
  入口迁 txecs 这件事本身就证明「把通道绑死在某台机器」是脆的。
- **Apprise / ntfy / Gotify 之类**：只覆盖「简单文本推一条」这一档。这里实际要发的
  是带 PNG 的飞书交互卡片、企微 markdown，复杂消息仍要自己写，等于多一个依赖
  只省掉最简单的部分。
- **邮箱做上行**：延迟不可控、无同步返回、鉴权弱、退信静默。


⚠️ **过时结论（2026-09-05，已被后续验收取代）**：早期真实客户端曾显示「请升级至最新版本客户端」，
因此当时暂时保持 JSON 1.0。后续 `preview7` 至 `preview10` 在当前接收群已真实可见，
并已按 JSON 2.0 正式部署；今后的切换门槛仍是 API 接受、真实消息肉眼可见和同 schema 的 PATCH 更新。

### JSON 2.0 早期现场验证记录（2026-09-05，历史证据）

- 未部署代码用 bridge 生产凭据向通用告警群发送 2.0 卡片：API 返回 `code=0`，消息 ID
  `om_x100b668cda11f4a0c322ef1e3266394`。
- 对同一条 2.0 卡片执行 PATCH：API 返回 `code=0`，读取结果 `updated=true`。
- 两次 GET 的 `body.content` 都是「请升级至最新版本客户端，以查看内容」，没有出现卡片
  正文。⚠️ 该结论后来确认是**取数方式错误**：当时没有带
  `card_msg_content_type=user_card_content`，接口返回的是兼容展示结构，不是发送时的原始卡片，
  不能据此判定客户端渲染失败。
- 因此不要把这条历史 GET 结果当成低版本客户端证据，也不要据此回退到 JSON 1.0。

### JSON 2.0 正式切换与 API 原始卡片验收（2026-09-05）

- 用户确认生产飞书客户端为 **7.76**，手工收发回复正常。
- `release-deploy.yml` run `33942500243`（business-cn）和 `33942655864`
  （bridge-peer）均成功；backend-api、bridge 容器均 healthy。backend 当前镜像为
  `backend-api:t-8dc4f32ca037`，business manifest 的 deployment 为
  `224e6d24963c-33942500243-1`；bridge 当前运行镜像 digest 为
  `sha256:7f17fd0543fd039f5c80e55de9d7e0d7bbf92fab237163eee2a0dd111b60bf0f`。
- bridge 真实新消息 `om_x100b668d449d48acc2cfc5ec74c00e4`：发送 `code=0`，同一条卡片
  PATCH 后读取 `updated=true`；带 `card_msg_content_type=user_card_content` 读取到原始卡片
  `schema=2.0`，正文为 PATCH 后内容。
- backend-api 真实新消息 `om_x100b668d5cd3d8a4ddab31c7f2af972`：发送 `code=0`；同样按
  `user_card_content` 读取到原始 `schema=2.0`、header、body 和 markdown 元素。
- 因而当前判据是：发送 API `code=0` + 原始卡片查询 `schema=2.0` + 同 schema PATCH
  `updated=true`。不带 `user_card_content` 的 GET 只用于兼容展示，不用于判断卡片是否可见。


### 正式部署记录（2026-09-05）

- AliECS 生产代码为 `27b7faa`（日报与 JSON 2.0 渲染）+ `e363c05`（bridge 同 schema、测试和渲染语义统一）。
- `release-deploy.yml` run `33941286766` attempt `2` 的构建、TCR 镜像同步均成功，peer job 按当前路由为 `skipped`。
- 该 run 的 GitHub runner 在 txecs 的 `docker compose` 重建 backend 瞬间取消了 SSH action；同一 run 上传的 release 已保留，随后在 txecs 现场用该 release 和同一 `deploy-role.sh` 完成部署。
- txecs `/srv/business-cn/state/release-meta/current.json` 已记录 deployment `e363c0571cb4-33941286766-2`；backend 使用 `t-8dc4f32ca037`，容器 healthy，`/health/` 通过。后续 session 先核对这个 manifest 和容器 tag，不要因 Actions 总状态为 cancelled 而重复发布。
- 收尾复核时发现另一条并行操作曾短暂把 `compose.env` 改回旧 backend tag `t-d12a5387870b`；已按 manifest 恢复 `t-8dc4f32ca037`，容器再次 healthy 且 15 秒观察未被覆盖。多 session 并行时，关闭前必须把 `compose.env`、容器 image 和 `current.json` 三者对账。
- ⚠️ 上述 run `33941286766` 及其旧 deployment 记录是切换过程中的历史证据，不是当前版本权威。当前以 run
  `33942500243` / `33942655864`、txecs 的 `current.json` 和容器实际 image digest 为准。


⚠️ **过时记录（不要照抄）**：2026-09-04 曾因只验证 HTTP 200、未做真实客户端验收，
把 JSON 2.0 判定为不兼容并回退 1.0；2026-09-05 的 `preview7` 至 `preview10` 已在当前
客户端真实可见，现行实现以本节约束为准。


## 三个通道的首次真实调用（2026-08-31）

上线时只有飞书 + 服务端内部生产者这一条路真的跑过；其余是按「代码就绪」交付的。
2026-08-31 逐条补验，结果记在这里，免得以后再问「这条路到底通没通」。

| 路径 | 首次真实调用 | 结果 |
|---|---|---|
| `POST /v1/internal/notify/send`（外部入口 + `notify_sources` token 鉴权） | 2026-08-31 | 通。此前 `notify_sources` 是空表、nginx 全量日志 0 次请求，**任何调用都必然 401** |
| `wecom_bot`（群机器人） | 2026-08-31 | 通，含 markdown、图片、4096 字节截断 |
| `wecom_app`（自建应用） | 2026-08-31 | **通道代码通，但生产 env 的 agentid 是错的**，见下 |
| 重试 → `dead` 全链路 | 2026-08-31 | 通，四次尝试全程实测 |

`/send` 的四种返回都实测过：缺 header 401、错 token 401、`source` 与 header 不一致 403、
无匹配路由 200 + `delivered:false`、正常 200 + `delivered:true`、重复提交 200 + `duplicate:true`。

实测当天 `routers/notify.py` 里还留着一句「返回 202 而不是 200」的注释，与实际不符
（装饰器没有 `status_code`，无路由时返回的是 **200**）；#342 已把那段重写掉，
现在注释与行为一致。本文档上面「三种 200 要分开读」始终是对的。

⚠️ 重复提交的返回在 #342 之后变了：以前一律 `duplicate:true` + `targets:0`，
现在会带上该 outbox **当前真实的**投递汇总（`sent`/`pending`/`dead`），
`sent>0` 时甚至报 `delivered:true`。所以「`duplicate` 就等于没发」这个旧理解不再成立。


**待办（截至 2026-08-31 未完成）**：gold-spread-monitor 的 422 还没抓到真实样本——
部署（17:03 CST）之后一直没有新的 gold 请求。已知的形状：
`monitor` 每轮可能出现 3×422 + 200，**但不是每轮都有**（11:14 CST 那轮有、
15:37 CST 那轮只有 200），所以它是间歇的、跟 payload 种类相关。
根因机制已定位一半：`gold-spread-monitor` 的 `alerts.py::_try_post` 用
`for attempt in range(3)` 把 422 当可重试错误重试 3 次——4xx 是确定性错误，
重试必然全失败，只是把一次失败放大成三条。**缺的是「哪个字段」**，等上面那条日志。


### ⚠️ 企微 agentid：env 里 A 的值是 B 的

2026-08-31 用各自的 `APP_SECRET` 调 `agent/get` 实测：

| profile | 正确 agentid | 应用名 | 容器 env 现状 |
|---|---|---|---|
| `COMPANY_A` | **1000005** | AGI | `WECOM_COMPANY_A_gentId=1000003` ← **错的** |
| `COMPANY_B` | **1000003** | AGI-达 | `WECOM_COMPANY_B_gentId=` ← 空 |

拿 A 的 token 去操作 1000003 会被拒：
`301002 not allow operate another agent with this accesstoken`。
也就是说**只要有任何路由指向 `wecom_app` + `COMPANY_A`，投递必然失败**（会走完
四次重试判 dead）。注入正确的 1000005 后同一段代码真实发送成功。

**正确的值其实一直躺在 SOPS 里**：还有一个从来没人读过的
`WECOM_COMPANY_A_gentId_A=1000005`。同一个键被两种方式改残，compose 恰好只转发了
错的那个（`gentId`），而 `app_credentials()` 的 typo 容错又刚好认它——三层各自「正常」，
合起来就是 A 拿着 B 的 agentid。

已修（2026-08-31）：SOPS 两个文件（`txecs.enc.env` / `txecs-production.enc.env`）各加
`WECOM_COMPANY_A_AGENT_ID=1000005`、`WECOM_COMPANY_B_AGENT_ID=1000003`，compose 转发这两个
规范键，`app_credentials()` 优先读它们。历史拼写 `gentId` / `gentId_A` 保留不动，只作回落。

`tests/test_notify_center.py::WeComChannelTests::test_canonical_agent_id_wins_over_the_historical_typo_key`
守着优先级——原来那个测试只验「typo 键能被读到」，**读到的是不是对的值它不关心**，
所以这个错配从上线一直活到第一次真实调用。


### 首次真实调用（2026-09-01）

| 项 | 结果 |
|---|---|
| 设备侧 `aliecs-traffic` → `/v1/internal/notify/send` | **通**。`delivered=True`，`notify_deliveries` 里 `status=sent, attempts=1`，用户在飞书运维群确认收到 |
| 心跳缺席看护（接收端） | **代码已上生产**（`doc-sync-worker` 镜像 `t-fdc345c9d06f`），但**开关仍关闭**，因此这条路径**一次都没有被真实触发过** |

⚠️ 按〈代码就绪 ≠ 这条路真被调用过〉，看护那一栏现在是空的。开开关之后必须造一次真实的
「心跳缺席」（把 `notify_outbox` 里 aliecs-traffic 的最新一行时间改旧，或停设备侧 timer 满
30 小时），确认它真的报出来，再把结果补进上表。**在那之前不要认为这条反向看护是可用的。**


### 2026-09-11 首次上线验证

AliECS PR #386 已合并为 `d74bfe9`，并由 release workflow `34551326608` 部署到
`business-cn`；迁移 `0061_txecs_traffic_notify` 已应用。数据库回读确认
`txecs-traffic` 来源、同群 `feishu` 路由均存在。txecs 首次 `traffic.daily_report` 返回
`delivered=True`，对应 `notify_deliveries` 为 `status=sent`、`attempts=1`；设备 pending
队列为空。用户已确认在飞书群收到消息。

这次验证的是日报正常路径；没有人为伪造 50%/75%/90%/100% 档位，因此阈值消息仍应在
真实跨档时观察。首个腾讯云套餐周期结束后，再将 vnstat 的 eth0 TX 与云账单对账。


### 2026-09-06 日报漏发复盘

2026-09-06 00:07（CST）设备 timer 正常运行，但日报在进入 `notify_outbox` 前被
backend-api 返回 HTTP 422：`body.segments[0].fields[0]` 收到的是数组
`["出方向", "**2.78 GB**"]`，而 `NotifyField` 契约要求对象
`{"name": "出方向", "value": "**2.78 GB**"}`。这是 JSON 2.0 `section` 生产者与中枢
模型的 schema 不一致，不是 Feishu 客户端兼容性问题。由于 422 被正确判定为 payload 永久
非法，设备不会写 pending；补发时要重建原 dedup key，并同时检查 `notify_outbox` 和
`notify_deliveries.status=sent`。以后修改卡片 schema，必须在设备脚本测试之外执行一次
backend-api `Notification.model_validate()`，再做真实投递验证。


### openclaw-bridge 运维告警

bridge 的多维表格配置变更和每日核对告警使用来源 openclaw-bridge、事件
bridge.alert，优先走 POST /v1/internal/notify/send。txecs 上 bridge 是
network_mode: host，因此 endpoint 使用 backend-api 发布的
http://127.0.0.1:8000/v1/internal/notify/send，不要写 Docker 服务名。

中枢请求设有短超时；连接失败、超时或配置类 HTTP 错误会静默回落现有 Feishu 直发，
不能影响发现问题的线程。HTTP 502 表示 outbox 已建立并等待重试，不再直发，以免中枢
恢复后重复发送。回落路径仍捕获所有异常。

启用前按顺序完成：先写来源和路由，再渲染 bridge env，最后重建 bridge 容器。token
明文只在执行 INSERT 时存在，数据库只保存 sha256，路由不保存任何凭据：

SQL：
INSERT INTO notify_sources (source_key, token_sha256, note)
VALUES (
  'openclaw-bridge',
  encode(sha256('<明文 token>'::bytea), 'hex'),
  'openclaw-bridge 运维告警；token 在 infra secrets/txecs-bridge.enc.env'
);

INSERT INTO notify_routes
  (source_key, event_pattern, min_level, channel, target_json, note)
VALUES (
  'openclaw-bridge',
  'bridge.alert',
  'info',
  'feishu',
  '{"profile":"COMPANY_A","receive_id":"oc_84d1130542509e374f7ea20c13d11ca4","receive_id_type":"chat_id"}',
  'bridge 运维告警'
);

重复执行前先按 source_key 和 (source_key,event_pattern,channel,target_json)
查重；数据库命令使用 psql -U app -d app。bridge 的 env 键为
NOTIFY_CENTER_ENDPOINT、NOTIFY_CENTER_SOURCE、NOTIFY_CENTER_TOKEN 和
NOTIFY_CENTER_TIMEOUT_SECONDS，其中 token 只从 SOPS 渲染。

#### 当前生产状态（2026-09-05 收尾核对）

- 代码已由 `eca3fa2` 合并到 `main` 并随 bridge 发布；`send_feishu_alert()` 现在优先走中枢，
  失败时静默直发回落。
- txecs 运行时已确认 endpoint 为
  `http://127.0.0.1:8000/v1/internal/notify/send`，source 为 `openclaw-bridge`，timeout 为
  2 秒；token 已渲染但不打印。
- 数据库已确认 `notify_sources.source_key='openclaw-bridge'` 为 enabled，且存在
  `bridge.alert` / `warn` / `feishu` / `COMPANY_A` 运维群路由。
- 本次收尾没有人为制造一条运维告警，避免额外刷群；因此“中枢真实成功 + 断链回落”的运行日志仍应在下一次自然告警或专门维护窗口补一条端到端证据。代码单测已覆盖 HTTP 成功、502 入队不直发、网络失败回落。


## notify 补充分层原文

投递代码只有 backend-api 一份。触发两条：HTTP 入口同步投递；worker 的 `_poll_once`
每轮调一次 `/v1/internal/notify/flush` 带走积压（**不是外层 while True**，理由见下文
「冲刷频率跟 poll 走」）。所以 worker 写的通知最长延迟一个轮询周期
（`DOC_SYNC_POLL_SECONDS`，默认 30s）——2026-08-31 上线实测 **34 秒**：
写入 → 领养 → 投递到飞书。

留下这个数字是为了以后判断得出「慢了」。只写「最长一个轮询周期」的话，
延迟涨到几分钟也看不出异常。

## notify 补充分层原文

- **企微 B 没有自建应用 agentid**。SOPS 里 B 只有 `WECOM_COMPANY_B_GROUPBOT_ID/SECRET`
  和 `APP_SECRET`，没有 agentid，所以 B 目前只能走群机器人。要用应用消息得先在企微
  后台建应用并把 agentid 进 SOPS。

  ⚠️ **「B 没有自建应用」这个说法自 2026-08-31 起确认不准确**：B 有自建应用
  （`agentid=1000003`，名称 `AGI-达`），只是 agentid 没进 SOPS，容器里
  `WECOM_COMPANY_B_gentId` 是空字符串。用 B 的 `APP_SECRET` 调
  `agent/list` 就能读到。缺的是配置，不是应用。


## notify 补充分层原文

  ⚠️ **2026-08-31 之前实际不是这个序列**：`mark_failed` 取的是
  `BACKOFF_SECONDS[attempts]`，而 `attempts` 在取值前已自增，于是第一次失败等的是
  **300 秒**而不是 60，首档 60 永远取不到，实际序列是 `300 → 1800 → 7200 → dead`。
  生产实测过（2026-08-31 outbox 10：失败 02:49:30 → 重投 02:54:40，间隔 310 秒）。
  已改成 `BACKOFF_SECONDS[attempts - 1]` 并把 `MAX_ATTEMPTS` 提到 `len+1`，
  由 `tests/test_notify_center.py::RetryAccountingTests::test_backoff_uses_every_tier_starting_at_the_first`
  守着——**那个测试断言的是具体秒数**，原来的测试只断言 `status=='pending'`，
  在正确和错误索引下都成立，所以它没挡住。


## notify 补充分层原文

- **`send_feishu_text` / `send_feishu_alert` 的返回值语义变了**。收敛前 `False` 是
  「飞书没收到」，收敛后是「没能写进 outbox」。写进去之后投递成没成，只有
  `notify_deliveries` 知道——调用点如果拿这个返回值做业务判断，要重新审一遍。
