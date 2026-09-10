# Gold V6 三页面完善与性能验收执行计划

**Goal:** 在已部署三页基础上完成可登录、可用、轻量的实时行情、当日错单和历史复盘，并形成可复核的性能与上线证据。

**Architecture:** 沿用 FastAPI + PostgreSQL + 原生 JavaScript + 已有 Lightweight Charts。保留原始行情与事件证据；通过有界查询、按用途拆分响应、增量轮询和按需详情降低成本。复用已有认证与图表计算，禁止前端重新计算 V6 交易决策。

**Spec:** `docs/superpowers/specs/2026-09-10-market-three-page-split-design.md`。该文及旧实施计划是历史设计，当前目录结构和用户最新要求优先；本计划补全尚未实现的部分。

## 0. 接手边界与已完成事项

- 工作树 `/home/ishelwsl/src/aliecs-wt-v6-review`，预期分支 `codex/v6-visual-review-20260907`，本轮检查基线 `e2b52bdaa8089705832e301de15c756e6d1eebca`。执行前重新核对，不假设没有其他 session 写入。
- 所有 Git 命令必须显式 `git -C <实际工作树路径>`。若该树仍有人使用，在工作区 `_worktrees/` 下从这个交付分支建立独立分支，不从 main 丢弃既有功能。
- Gold 原仓 `/home/ishelwsl/src/gold-spread-monitor` 只读；不得覆盖其其他会话修改。若证据表明需修改 Gold 发布器，单列跨仓修复方案和证据，网站部分继续推进。
- 本计划授权边界为代码、测试及文档完善；不自动授权 commit、push、merge、生产迁移、部署或服务重启。收到明确授权后自行完成对应交付动作。
- 不连接真实资金账户、不下单、不撤单；保持 `execute=false`，不改密钥、生产配置和部署目标。
- 已有设计、页面、API、导航、目录路由修复和 today/history 的 token header 修复均应复用。不要重新搭脚手架或重做已完成的旧观察台修复。
- 三页真实路径是 `market/realtime/index.html`、`market/today/index.html`、`market/history/index.html`。不能恢复旧 `.html` 文件来迁就测试。
- 旧 `/market/` 保持可访问及已有功能，三页导航提供旧观察台入口；本次不擅自将其重定向或删除。

## 1. 已知证据及不能推断的结论

2026-09-10 本轮现场检查（执行时刷新必要的易变事实）：

- 本地与 origin 分支一致且干净。Actions `34461585329` 的业务部署成功，服务器 current/release JSON 指向 e2b52bd；运行 public-web tag `t-782d2cb46e93`，digest `sha256:e174bf9b47c661a46227b47a44e7d1f9b44228f5d6947928b5c7a020814d52c0`。两个 JS 和三个入口 HTML 的本地/容器 hash 已匹配。
- 三个公网入口匿名都是 302 → Authelia，响应体各 154 bytes。没有认证后市场页面证据。ChatGPT 登录态不等于 hydwang SSO 登录态；不要为了验收占用 ChatGPT/飞书/额度采集生产 Chrome。
- realtime.js 未携带 Authorization；也未消费 bands、orders、hedge_ranking。today/history 都调用忽略 scope 的 mount()，只加载最新 run 前 50 条事件，没有真正分页或当日/历史分界。
- 详情虽按 event_id 请求，但仅显示时间、事件类型、持仓号；未完成用户所需图表复盘。
- 当前前端测试 6 passed / 1 failed，失败位于 tests/test_market_frontend.py:62，原因是仍读旧 .html 路径。指定旧观察台浏览器用例 1 passed / 9.11s；历史 1764 passed 不代表当前 HEAD 全绿。
- DB 样本：最新 run 有 8 合约、60 条事件（不等于 60 笔独立错单）。10:03Z 时最大 observation 时间 07:18Z，最新快照 published_at 07:21Z、received_at 10:01Z；最近 15 分钟观察为 0。
- 上述时间落在北京时间傍晚。休市可以解释没有最近成交，但不能自动解释较旧 published_at 的晚到接收。必须区别休市、源停滞、历史补发、积压及网络/处理延迟，不能直接宣称采集故障。
- backend 直读和 healthcheck 有超时，日志出现单次 ingest 约 80–88 秒及批量请求随后完成。尚未证明根因是数据库、同步阻塞或发布器，也未证明由本次拆页引起。

## 2. 文件职责

| 文件 | 工作内容 |
|---|---|
| `services/backend-api/app/routers/market_snapshot.py` | 三类读取接口参数、鉴权、上限和兼容性 |
| `services/backend-api/app/market_review.py` | 有界 SQL、增量游标、索引过滤、单事件关联详情 |
| `services/public-web/market/realtime.js` | 八合约图表、增量合并、挂单、排序、刷新生命周期 |
| `services/public-web/market/review-detail.js` | today/history 控制器、筛选分页、选择及取消、详情展示 |
| 三个 `market/*/index.html` | 页面容器、状态、筛选、分页与共享资源引用 |
| `services/public-web/market/market.js`、`index.html` | 只提取确需共享的认证/绘图逻辑；保护旧观察台 |
| `services/public-web/market/market.css`、`observation.css` | 沿用样式并局部适配；避免全局样式影响旧页 |
| 可新增 `services/public-web/market/page-common.js` | 若现有 common auth 无法直接复用，封装 token/SSO、请求和状态；不可复制三份认证实现 |
| `tests/test_market_frontend.py`、`test_market_review_api.py` | 真实路径与协议/权限回归 |
| `tests/test_market_review_browser.py`、`test_market_review_storage.py`、`test_market_review_e2e.py` | 行为、数据库、端到端与性能边界 |
| 可新增 `tests/test_market_split_browser.py` | 独立三页场景，避免旧浏览器测试文件无限膨胀 |
| `db/migrations/` | 仅查询计划证明必要时增加可审查索引迁移；编号先核对，不修改历史迁移 |

## 3. 执行顺序与任务验收

### Task A：冻结基线、定位负载来源

- [ ] 读取实际 worktree 的 AGENTS、现有 spec/plan、部署 runbook；生产检查先读 fleet。记录 status、HEAD、remote 和已存在的改动归属。
- [ ] 复用项目 `.venv` 和 Chromium；先运行市场 API/前端相关测试，记录当前失败。修正三页契约中的旧文件路径，不放松行为断言。
- [ ] 对照旧 `/market/` 与新页，在相同合成数据、相同视口和缓存条件下记录请求数量、JSON 解码字节、传输字节、DOM、首屏及刷新时间。比较重复三轮，交替页面顺序。
- [ ] 只读排查 backend 延迟：容器 CPU/内存、healthcheck、有限时间日志；DB 只读事务 + statement_timeout，查看 pg_stat_activity 等待/阻塞、表索引及查询计划，避免生产全表 EXPLAIN ANALYZE。
- [ ] 分别记录 source_time、observed_at、published_at、received_at 和交易时段。如 async 路由内直接调用同步 DB/解析，提出并在本地慢依赖测试中验证其是否阻塞 health/API；未证实前不做大范围性能改造。

验收：每项缺口有复现、所在层、证据和对应测试；休市空窗口与异常停滞分别处理。性能故障调查不阻塞下面的本地前端/API实现。

### Task B：三页共用完整认证链路

- [ ] 复用旧页 SSO handoff 消费、同源 token 获取和安全返回 URL；审查 `/common/` 已有模块后决定是否提取 page-common.js。
- [ ] 所有读取（含 realtime、index、detail）走统一请求函数，携带 Bearer token，检查 HTTP 状态及 JSON Content-Type，识别被重定向到 HTML 登录页。
- [ ] 明确无登录、401 过期、403 无权限、网络超时、5xx 的页面状态；未登录提供正常登录入口，返回原页面。不得循环跳转、停在无限 loading 或显示伪空数据。
- [ ] 确认 cookie 的 SSO 与 API Bearer 两层认证均能完成。不要伪造 JWT、从生产密钥签发测试 token或绕过权限来声称浏览器验收通过。
- [ ] 测试：直接打开三页无本地 token、handoff 返回、有 token、过期、403、HTML 响应、刷新失败后恢复。日志/截图/HAR 均脱敏，不存完整认证 header。

验收：三页可从直接访问完成正常登录流程；已认证请求都有 Authorization；异常均有可操作状态。

### Task C：实时接口与八合约轻量显示

- [ ] 首屏 `GET /api/v1/market/realtime` 返回明确契约：`run_id, server_time, window_start, window_end, window_minutes=15, contracts, series, orders, hedge_ranking, freshness, next_cursor, truncated`。保留现有消费者依赖字段，或同批更新并测试全部新页调用者。
- [ ] `contracts` 按后端当前合约集合提供最新值及源时间，不按最近 2000 行决定哪些合约存在。`series` 每合约包含报价和价格带，固定真实时间横轴，禁止将索引序号当时间。
- [ ] 服务端强制 `[now-15m, now]`、总计最多 2000 个规范化图表观察点（quotes + bands 合计）；采用按合约保额及确定性时间桶，避免活跃合约挤掉其它合约。保留桶内首/末、高/低及各自时刻，缺口不跨线；标注采样与截断，绝不冒充完整逐笔。
- [ ] `after=<opaque cursor>` 返回新增/修正观察；游标绑定 run、窗口/版本及稳定序键，同时间点不漏数据。迟到上传、run 切换、无效游标有明确 reset/full-window 语义。最多每两秒一次，单请求在途、超时取消、失败退避；页面隐藏暂停，恢复时有限重取。
- [ ] 首次创建每合约图表，刷新增量 update、裁剪过期点，不每两秒 innerHTML 重建全部图表。销毁定时器、请求和监听器；相同值价格图不能出现 NaN 坐标，null 不能转为 0。
- [ ] 展示成交曲线、I 上下带及中心、当前有效买卖挂单（方向/价格/状态）、后端对冲候选排序。排序来源使用已有 quote.hedge_previews 等真实字段；先对齐生产数据契约，不能只等一个不存在的顶层 hedge_ranking。
- [ ] 缺排序显示原因/更新时间，缺源显示缺失；休市且无窗口数据仍显示带时间的最后已知快照，不把过期曲线挪到当前 15 分钟。
- [ ] 本地测试八合约不均衡更新、超过上限、平价、null、缺口、同秒更新、乱序/重复、迟到数据、run 切换、隐藏恢复以及网络故障恢复。

验收：实时页市场数据首屏只调用 realtime，无 latest、events、positions 或详情；八合约有数据时均有真实时间曲线，窗口和内存有上限，挂单及排序来自后端事实。

### Task D：当日/历史索引真实分流与分页

- [ ] 扩展 `GET /api/v1/market/events/index`：`scope=today|history, date_from, date_to, symbol, status, after, limit`；limit 默认 50、最大 200。保留现有 run_id 调用兼容性。首屏不调用重量级 latest 来获取 run。
- [ ] 明确错单索引单位：以触发错单的锚事件/position 展示一次交易，生命周期事件归入详情。复用既有事件分类及目标成交规则，不把 60 条底层事件说成 60 笔错单；索引保留 anchor event_id。
- [ ] 交易日采用发布事件中的 trading_day；结合已有夜盘交易日规则确定“当前交易日”，返回 effective_trading_day 及口径。禁止简单用 UTC 日期或自然日零点代替夜盘交易日，缺交易日数据应说明而非悄悄归类。
- [ ] today 只含当前交易日，history 排除当前交易日并支持日期/合约/状态；可跨 run 查询，不能仅选择最新 run。明确状态来自模型/账户何者，保留未解决敞口。
- [ ] 游标绑定筛选条件，排序使用稳定复合键（交易日/事件时刻/唯一 ID），并发新增下翻页不重复不漏；筛选改变重置游标。返回 `items/events, next_cursor, has_more, effective_trading_day`，不需全库 COUNT。
- [ ] UI 提供加载、空、错误、重试、下一页/加载更多；首屏最多 50 行，DOM 总量有限，不能无限追加。today 可低频刷新当前索引页，history 默认手动刷新，任何刷新不预取详情。
- [ ] 测试跨日/夜盘、多个 run、同刻事件、相同 position 生命周期去重、缺字段、51+ 条记录、筛选与分页、无权限和空库。

验收：today/history 的结果集有明确日期语义；历史页可读第二页和较旧 run；未选事件时详情请求为 0，索引响应无曲线/原始 payload。

### Task E：单事件完整有界复盘

- [ ] 复用 `/events/{event_id}/detail`，从锚事件定位同 run/position 的生命周期、目标/对冲合约与已有 position 账本，避免跨 run 混合或请求其它持仓详情。
- [ ] 首次选择恰好发起一个 detail 请求，包含所选事件附近默认前后各 5 分钟、最多 15 分钟、合计最多 2000 图表点。返回窗口、是否截断、缺口、实际数据时间范围及关联证据。
- [ ] 生命周期上限 200 条，超限返回显式 has_more/cursor；只有用户主动查看更多才继续请求。长持仓使用阶段按钮选择窗口，不一次拉取全日曲线。
- [ ] 展示目标/对冲两图、I 带、当时挂单与成交标记、冻结的决策候选排序、进出场阶段、模型/账户分栏、费用收益及未解决敞口。未知收益为“—”，缺平仓回报不得按零敞口处理。
- [ ] 复用旧观察台已验证的坐标、排序、账本投影和缺口算法；详情默认不输出原始 JSON，全量证据仅明确点击后有界展示。
- [ ] 快速从 A 选择 B：取消 A 请求或用选择序号丢弃迟到 A；重复点同一事件使用小型有界缓存；切页清理图表和请求。
- [ ] 测试首屏 0 detail、点击一次 1 detail、不加载无关事件、快速切换、404/403/5xx、未解决/已平仓、超窗口/超行数、两个 run 相同 position_id。

验收：选择一笔能复盘其发生和处理过程；摘要、图表和账本属于同一事件，缺失证据如实显示。

### Task F：性能、回归和交付

- [ ] 在相同八合约 fixture 下验证建议性能预算：realtime 首屏 JSON 解码后 ≤500 KiB，正常增量 ≤100 KiB，50 行索引 ≤50 KiB，单详情 ≤500 KiB；超预算先分析字段和重复信息，不静默删除必要证据。
- [ ] 本地固定环境连续 30 次读取，热数据 realtime/index p95 ≤500ms、detail p95 ≤1s。首次业务可见内容在响应完成后 ≤1s。记录机器/fixture/缓存条件；线上另记网络延迟，不能混用。
- [ ] 真实浏览器连续 10 分钟轮询：最大在途 1，图表数/DOM/缓存不持续增长；窗口数据有上限，隐藏页不继续轮询。记录首屏和第 10 分钟数据点数、DOM、请求数、堆趋势及长任务。
- [ ] 覆盖 390/1366/1920 宽度，无横向溢出，图表位于可见容器，状态/筛选/详情可操作。
- [ ] 先运行下面专项；有 DB 的测试使用隔离测试库，禁止测试连接生产库。明确 passed/skipped，pytest 绿灯不代表 skipped 的真实 DB 测试通过。

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_market_review_api.py tests/test_market_frontend.py tests/test_market_review_browser.py tests/test_market_review_storage.py tests/test_market_review_e2e.py -q
```

- [ ] 如新增 split browser 文件，把它加入专项。专项通过后执行现有全量命令一次，保留必要失败摘要，不机械重复全量：

```bash
PYTHONPATH=.:services/tplus-sync-worker:services/tplus-sync-worker/src:services/backend-api .venv/bin/pytest -q
git -C /home/ishelwsl/src/aliecs-wt-v6-review diff --check
```

- [ ] 更新实施记录 `docs/superpowers/plans/2026-09-10-market-three-page-acceptance.md`：实际 HEAD、修改文件、命令/结果、性能表、截图/脱敏证据位置、已知限制和发布操作。证据文件放 /tmp，不提交浏览器数据、token、日志和 output。
- [ ] 对旧设计/计划中已失效的路径、旧入口重定向及“已完成”表述原地注明日期并链接本计划；不要抹掉历史记录。
- [ ] 收尾运行工作区 `python3 scripts/check_worktrees.py`；只负责自己修改的仓，保留其他 session 中间态。若脚本内部 Git 不符合显式路径约束，先说明并使用等效显式路径只读核验。

## 4. 获授权后的上线闭环

- [ ] 提交前核对分支、remote、全部 staged diff 与敏感文件；所有 Git 写操作串行。遵守网站仓 PR 规则及用户明确授权的分支交付方式。
- [ ] 数据库迁移如必要，单独说明锁、兼容旧服务及回滚策略；不默认生产执行。前端/后端契约兼容后按既有 business-cn 部署流程上线，不改部署架构。
- [ ] 核对 Actions 目标 job、current commit、run/attempt、实际容器 digest、页面文件 hash；不能以工作流总绿代替这些证据。
- [ ] 使用用户已登录且用于市场验收的浏览器，记录三页主文档 200、API Authorization 存在（不记录值）、状态、解码/传输字节、合约数、窗口范围、索引数量、详情点击前后请求数。
- [ ] 覆盖真正的第二页、历史筛选、选择两条事件及未解决敞口；数据不存在则明确未验证，并以本地合成证据补充，不能冒称真实业务通过。
- [ ] 活跃交易时段验证新鲜度和连续增量；休市验证“无近15分钟成交/最后已知时间”的正确提示。不等待休市自动产生数据，不用历史数据假充实时。
- [ ] 如果缺可用登录浏览器，只暂停依赖该登录态的线上验收；先完成所有本地交付及服务器只读检查，向用户说明具体缺哪个入口。不得借用 ChatGPT 生产会话做市场验收。

完成判据：代码/测试完成、真实性和性能预算有证据、授权范围内交付闭环完成；未授权部署、缺登录态、休市未测活跃数据分别列出，不能混写成全部完成。
