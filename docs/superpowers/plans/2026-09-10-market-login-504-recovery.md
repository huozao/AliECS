# Gold V6 登录 504 与慢快照写入修复执行计划

目标执行 session：`01a08af5-7c6c-7083-a1c1-2f3090334d1a`。

目标：恢复三页登录及动态读取，阻止市场慢写入拖住网站其它请求；定位并修复快照写入高耗时，完成真实登录链路与负载回归。延续三页完善成果，不重做脚手架。

## 一、当前证据与结论边界

本次只读检查时间：2026-09-10 19:37 左右，UTC+08。

1. Nginx error.log：19:29:12 `/api/v1/auth/oidc/login` 发生 `upstream timed out ... while reading response header`，upstream 是 `127.0.0.1:8000/v1/auth/oidc/login`；19:27:15、19:32:46 `/api/v1/auth/oidc/callback` 同样超时。同期市场 `/latest` 超时。日志包含用户授权 code/state，后续输出或保存必须去掉查询参数，不能复制原始日志到交付文档。
2. backend 运行单进程 `uvicorn app.main:app --host 0.0.0.0 --port 8000`。healthcheck 多次超过 20 秒，中间成功一次，故 Docker 的瞬时 healthy 不代表持续响应正常。
3. `routers/market_snapshot.py:215` 的 async 接收路由在读取 body 后直接调用同步 `market_review.ingest(body)`；后者同步 psycopg 连接、事务、INSERT、触发器及 advisory lock。慢同步调用占据事件循环会使该 worker 无法及时接收/调度其它请求。这是已确认代码缺陷；与本次具体超时的因果关系应以运行文件核对和本地受控并发复现补证。
4. 生产 pg_stat_activity：快照 INSERT 已运行约 72 秒，采样时 wait_event 为空、pg_blocking_pids={}。上一 session 曾采到 IO/DataFileRead，说明可能有读盘成本，但不能断言所有慢请求都在等待 I/O 或行锁。
5. 生产 AFTER INSERT 触发器仍逐条调用 `market_review_advance_observation`，每条处理 JSON、观测投影和 current 表 UPSERT。相关 PK 和窗口索引已存在；没有证据支持盲目补重复索引。
6. 另一条 external_sources 事务 idle 约 449110 秒，但不是当前快照 INSERT 的已证实 blocker。不要杀掉它或将它直接当根因；如需治理，独立报告持有者和影响。
7. 新 page-common.js 登录先去主站 OIDC login，再经 Authelia callback 和市场 handoff。用户能看到静态页、随后在登录入口或回调失败，与上述 backend 超时一致。不是简单的匿名 302 问题。
8. realtime.js catch 中 `login.hidden = error.cause === "login"` 在 401 时隐藏了登录按钮；文字写“退避重试”，实际仍 setInterval 每 2 秒请求。这些是独立可修前端缺陷，不是 Nginx 504 的解释。
9. OIDC `_pending_states` 是进程内 dict。直接加 uvicorn workers 会导致 login 与 callback 落在不同进程，产生随机 invalid state；本次优先保留单 worker。

## 二、工作边界与上下文

- 工作树 `/home/ishelwsl/src/aliecs-wt-v6-review`，分支 `codex/v6-visual-review-20260907`，Git HEAD 基线 e2b52bd。当前有上一 session 的未提交代码及新增 page-common.js、测试和 acceptance 文档；必须保留，并在原 session 内续做。
- 所有 Git 使用显式 `git -C <路径>`，所有 Git 写操作串行。Gold 原仓只读，保持 execute=false，不连接账户、不交易。
- 阅读当前 worktree AGENTS、三页 completion/acceptance 文档，以及工作区 fleet/deploy runbook。线上已经热更新，镜像标签不能完整代表容器内容：对实际文件 hash 和进程加载状态验收。
- 本计划不执行任何生产修改。目标 session 继承它自身原有的用户授权，核对授权范围后使用；不得仅从这份计划推定有数据库迁移、清理事务、全栈重启权限。未获授权的外部操作应在本地结果可审查后列明。
- 修改文件预计：`routers/market_snapshot.py`、`market_review.py`（如需事务预算）、`page-common.js`、`realtime.js`、`review-detail.js`，以及相关测试；只有证据支持才新增数据库迁移。

## 三、执行步骤

### A. 保存最小故障基线与补齐运行态证据

- [ ] 核对 status/HEAD，记录本轮开始前已有改动。核对运行中的 market_snapshot.py 与本地相同；仅打印 hash 和相关代码片段。
- [ ] 在同一时间窗口记录 Nginx 登录/回调路径的状态与 upstream、backend health 日志、pg_stat_activity 的 PID/耗时/等待类型/blockers；脱敏路径查询参数。
- [ ] 只读检查 Nginx 登录 location、upstream 地址及现有 timeout。检查 backend 到 OIDC discovery 的访问是否有独立 DNS/TLS/HTTP 问题，仅输出状态/耗时，不输出 secret。
- [ ] 用受控本地 ASGI/uvicorn 测试建立单 worker 基线：patch 同步 ingest 为 threading.Event 等待，启动 ingest 请求；在另一个线程/客户端请求无需 DB 的探针及 OIDC login（discovery 使用固定 fixture）。修复前在 ingest 释放前不能响应，修复后应能响应。
- [ ] 测试用独立计时线程负责在上限内释放 Event，避免测试自己的事件循环被卡死。不能只并行调用两个 Python 函数或使用不同 TestClient 事件循环来证明生产单 worker 隔离。

验收：明确区分数据库慢、事件循环受阻、OIDC 外呼慢；不以一次 curl 成功推翻间歇性阻塞。

### B. 优先隔离市场同步工作，恢复登录调度

- [ ] 网络流读取保留 async；同步 gzip/JSON 大负载处理、V6 validate/ingest、旧 snapshot 原子写盘分别审查其是否阻塞事件循环，把有界同步阶段放到受控执行器。
- [ ] 最小方案可用已有线程 offload 能力，但必须给市场写入专用容量限制，不能让同 run 的 advisory lock 等待占满 FastAPI 共享线程池。优先单个活动写入、少量有界排队，或满载及时 503 + Retry-After；多 run 扩展前先有测试和吞吐依据。
- [ ] 容量限制必须覆盖任务实际运行周期。HTTP 超时/客户端断开不意味着后台线程已停止；禁止取消 await 后提前释放名额并启动更多写入。连接在工作线程创建/关闭，不跨线程共享游标。
- [ ] 不用 fire-and-forget 写入，不在事务提交前返回成功/ack。事务失败必须回滚；重试仍遵循不可变 run+sequence/event_id 幂等校验，不丢目标成交/账本事件。
- [ ] 分清数据库 connect_timeout 与查询预算。现有 connect_timeout=3 不限制已连接后的慢 INSERT。先测量正常耗时，在市场事务局部设置 statement_timeout/lock_timeout，预算小于上游超时并留回滚余量，不改全站 DB 默认值。
- [ ] 预算不能只是把每条正常但慢的写入永久取消。若正常批次始终超过预算，先完成 D 的写入成本修复或合理拆分；不得通过返回成功、吞事件或删除观测缓解。
- [ ] 检查 Gold 发布器当前只读代码中的 503/Retry-After、重试与未确认队列行为。如不能正确保存重试，先报告跨仓约束，不启用会丢数据的限流策略。
- [ ] 补充结构化阶段耗时：请求排队、解析、连接、advisory lock、INSERT/触发器、事件/position 投影、commit；只记录数量、耗时和安全标识，不记 payload/token。

验收：慢 ingest 未完成时，单 worker 登录入口仍可返回 302；有界并发不会耗尽认证读取资源。此阶段只证明隔离恢复，不等于写入性能已解决。

### C. 修正登录与失败重试的前端行为

- [ ] 401 清理无效 token 并显示登录入口，停止无意义的自动重试；403 显示权限说明。无 token、handoff 失败、HTML/302、网络超时、5xx 分别处理，不能所有错误均提示“请登录”。
- [ ] 修正 realtime 的 login.hidden 反向条件；检查 today/history 同类逻辑。登录跳转失败要能重新发起全新流程，不复用失效 code/state。
- [ ] 为 handoff POST 添加有限超时和状态检查；清除 fragment 以避免泄露，提交前保留必要的 browser verifier 生命周期；验证网络不确定结果下安全重新登录，不重放一次性授权码。
- [ ] setInterval 改为请求完成后调度的单次 timeout。网络/5xx 使用有上限的指数退避与少量 jitter，成功恢复基准周期，尊重 Retry-After；隐藏页面停止，显示后受控恢复。文案与实际调度一致。
- [ ] 不在页面里通过 fetch 跨域模拟 OIDC 导航，不放宽 return URL 白名单，不移除 PKCE、state 或 handoff verifier。

验收：401 时按钮可见，超时不会无限 loading 或每两秒持续打满后端，登录成功返回原三页地址并发送 Bearer。

### D. 定位并优化 INSERT 的真实成本

- [ ] 使用隔离测试 PostgreSQL，确认 fixture 支持 V6_TEST_POSTGRES_CONTAINER/V6_TEST_DATABASE_URL 并使用已有本地容器；缺宿主映射不等于不能测试，不可把生产库当测试库。
- [ ] 只读取生产元数据与有界样本统计：单包 quote/band 数、JSON 大小、嵌套历史数组大小、观测/current 表与 TOAST 大小、dead tuples/autovacuum 统计、事务 xmin、容器 CPU/IO。查询设置短 statement_timeout，避免全库 JSON 展开。
- [ ] 在隔离库生成相同数据形状和必要规模，分阶段测量 INSERT、触发器投影、逐条 current UPSERT、历史字段重复存储。使用 EXPLAIN (ANALYZE, BUFFERS) 只在隔离库确认热点。
- [ ] 若逐行投影是热点：评估集合式插入/更新或最小函数改写；必须保留 source_time 与 observed_at 双时钟不回退、同秒序号规则、空时间规则、迟到/重复不可变证据。按顺序非支配更新不能未经证明替换成“取最大一行”。
- [ ] 若是 TOAST/JSON 历史重发、磁盘压力、缺索引或 vacuum 延迟，各自提供测量依据后最小修复；不盲加已存在索引，不全表重建，不删除历史，不擅自终止那条 idle 事务。
- [ ] DB 函数变更用新编号迁移，保留旧版本回退方案；在隔离库测试迁移前后与旧写入器兼容、事务原子性、幂等、异常回滚和连续多包吞吐。

验收：正常发布速率下积压不持续增加；记录单包行数/字节、写入 p50/p95/max、各阶段成本、端到端 published→received 年龄。单次样本不能声称 p95。

### E. 回归与获授权后的线上验收

- [ ] 新增 `tests/test_market_ingest_concurrency.py`：慢写期间 login 可用；并发满载明确拒绝或有界排队；客户端断开不释放仍执行的工作；异常回滚和重试不重复。
- [ ] 使用现有 `tests/test_backend_oidc_login.py`、`tests/test_auth_handoff.py`、`tests/test_market_snapshot_contract.py`、`tests/test_market_split_browser.py`，覆盖 PKCE/state、一次性 handoff、gzip/体积上限、401按钮、退避、3页正常认证与按需数据。
- [ ] 在真实隔离 PostgreSQL 跑 storage/e2e，不能把 skipped 写成通过。验证通过后再跑现有市场回归与项目全量；不为等待断言通过随意增加 sleep 或删除断言。
- [ ] 持续 10 分钟模拟实际写入+读取，记录线程/连接/排队上限、login/realtime 延迟和错误率。隔离测试中以慢 ingest 仍在执行时 login 1 秒内返回为调度隔离目标，真实外呼耗时另计。
- [ ] 生产应用前，按目标 session 已有授权范围列出精确文件、必要服务重启和迁移。不能以重启后暂时 healthy 作为修复，不增加 workers 或把 Nginx timeout 改长来掩盖问题。
- [ ] 热更新后的源文件与容器 hash、导入进程、新测试/提交状态一并记录；后续正式镜像交付应包含全部热更新，不能让下一次重建丢失代码。
- [ ] 用用户市场专用登录浏览器验收：market 静态页 → hydwang OIDC login 302 → Authelia → callback 成功 → 原 market 路径 → handoff 200 → realtime/index/detail 200。callback 通常是跳转响应而非固定 200，按现有浏览器绑定交接协议验收。
- [ ] 全程只保存路径、状态、耗时、字节数及 Authorization 是否存在，脱敏 code/state/token/cookie/verifier。不要自动代输密码/2FA，也不要占用 ChatGPT 生产浏览器。
- [ ] 登录验收须在持续真实市场写入期间完成，并验证 callback/handoff/DB读取也可响应；仅 login 302 不代表整条认证链路恢复。
- [ ] 更新原 acceptance 文档：明确时间和热更新状态，修正顶部“没有部署/重启”的过时总述，保留原阶段记录并链接此计划。列出实际 passed/skipped、延迟、残留限制。

## 四、禁止作为结案证据的捷径

- 静态 200、匿名 302、Docker healthy、仅重启成功。
- 把单次 IO 等待当全部根因，或未检查 blockers 就杀事务。
- 单纯加大 Nginx timeout、增 worker、无界线程化。
- 给用户让其反复登录而不修 backend 阻塞；每次重启会清空进程内 OIDC state，已开始的登录可能需重走。
- 仅凭 mock DB 测试声称真实写入吞吐恢复。

交付顺序：A复现 → B隔离与C前端修复 → D写入成本修复 → E回归和授权范围内上线。只有持续写入下的完整登录和动态读取均有证据，才能报告网站恢复可用。
