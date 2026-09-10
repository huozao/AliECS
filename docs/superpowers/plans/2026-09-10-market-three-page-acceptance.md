# Gold V6 三页面完善验收记录

日期：2026-09-10；最后更新：2026-09-11。工作树：`/home/ishelwsl/src/aliecs-wt-v6-review`；起始 HEAD：`e2b52bdaa8089705832e301de15c756e6d1eebca`；分支：`codex/v6-visual-review-20260907`。本记录覆盖本地代码、隔离 PostgreSQL、合成浏览器和已授权的 txecs 热更新；各节明确其对应的提交、推送或数据库迁移状态。

## 后续接手入口

先读 `docs/project-ai-map.md` 的 market 段落，再读本文、`2026-09-10-market-three-page-completion.md` 与 `2026-09-10-market-login-504-recovery.md`。代码边界是 AliECS 工作树；Gold 原仓保持只读，`execute=false`。生产的手工热更新已包含本次 `market_review.py` 详情响应收敛，正式 GitHub 发布仍应以本分支提交、合并 `main` 后的手工 `release-deploy.yml` 为准，不能把容器内文件当成唯一源码。新数据库迁移、清理 idle transaction、扩大 worker 数或修改 Gold 发布器重试都不在已授权范围内。

## 实施内容

- `services/backend-api/app/market_review.py`：`realtime_view` 现在固定 15 分钟、最多 2,000 条观察、按 run 的不透明增量游标、窗口/新鲜度/截断说明；`event_index` 支持 `today`、`history`、跨 run、交易日、日期/合约/状态筛选、稳定游标和每持仓一条锚事件。旧的 `run_id + after=<数字>` 调用仍返回 `events` 与 `next_sequence`。
- `services/backend-api/app/routers/market_snapshot.py`：实时和索引路由暴露上述有界参数，并保留 `market.read｜市场读取权限`。
- `services/backend-api/app/routers/market_snapshot.py`：市场同步写入使用单容量专用执行器；事务完成后才确认，满载返回 `503｜暂时繁忙` 与 `Retry-After`，客户端取消不会提前释放容量。
- `services/backend-api/app/market_review.py`：市场事务设置局部 `lock_timeout=3s`、`statement_timeout=15s`，并记录不含 payload/token 的校验、锁、触发器、投影、提交分段耗时。
- `services/backend-api/app/main.py`、`services/backend-api/Dockerfile`：访问日志只保留 path；正式镜像默认关闭 Uvicorn access log，避免认证 query 写入日志。
- `services/public-web/market/page-common.js`：三页共用浏览器绑定 SSO handoff、Bearer token、401｜登录过期、403｜无权限、HTML 登录页、超时和服务器错误处理。
- `services/public-web/market/realtime.js`：首次只读 realtime；以 cursor 合并增量、固定 15 分钟内存窗口、每合约一次性创建 Lightweight Charts，隐藏时暂停、恢复时重取，并显示 I 价格带、有效挂单、后端对冲排序及发布/接收时刻。
- `services/public-web/market/review-detail.js` 与三个入口：today/history 首屏只取索引，选择后才读取一条详情；详情展示同一运行编号/持仓的生命周期、模型/账户账本、未解决敞口，以及目标/对冲有界曲线和 I 中心带。三页均保留旧 `/market/` 入口。
- `tests/test_market_split_browser.py`：真实本地 Chromium 覆盖 realtime 单端点、today 详情延迟、历史 401/403 登录入口；`test_market_frontend.py` 改为真实目录入口；旧观察台的异步刷新断言等待加载结束，避免计数竞态。
- `tests/test_market_ingest_concurrency.py`、`tests/test_market_ingest_load.py`、`tests/test_backend_access_log.py`：覆盖单 worker 慢写登录、满载与取消、持续写入读取、认证 query 脱敏。

## 本地验证

| 命令 | 结果 | 说明 |
|---|---:|---|
| `PYTHONPATH=. .venv/bin/pytest tests/test_market_split_browser.py tests/test_market_review_api.py tests/test_market_frontend.py -q` | 14 passed，2 warnings，3.30s | API、路径、401/403 认证请求与三页 Chromium 行为 |
| `PYTHONPATH=. .venv/bin/pytest tests/test_market_review_browser.py -q` | 32 passed，59.95s | 旧 `/market/` 完整 Chromium 回归；刷新计数断言等待加载结束后稳定 |
| `PYTHONPATH=services/backend-api .venv/bin/pytest tests/test_backend_oidc_login.py tests/test_auth_handoff.py tests/test_market_snapshot_contract.py tests/test_market_ingest_concurrency.py tests/test_backend_access_log.py -q` | 34 passed，4 skipped，3.96s | PKCE/state、handoff、gzip、单 worker 隔离、取消占位、401/403 与日志脱敏；4 个依赖未配置的隔离项仍标 skipped |
| `PYTHONPATH=services/backend-api .venv/bin/pytest tests/test_market_ingest_concurrency.py tests/test_market_snapshot_contract.py -q` | 9 passed，2 warnings，2.39s | 慢写期间 login 302；满载 503；取消不释放执行容量；ack 仍在执行函数返回后 |
| `PYTHONPATH=. .venv/bin/pytest tests/test_market_review_storage.py::test_isolated_snapshot_projection_cost_evidence -q -s` | 1 passed，1 warning，2.19s | 隔离临时 schema：20 包、每包 40 quote+40 band、8,786 bytes；触发器写入 p50 40.248ms/p95 52.597ms，无触发器 p50 20.771ms/p95 24.793ms |
| `node --check` 三个 JS；`.venv/bin/python -m py_compile` 三个后端模块；`git diff --check` | exit 0 | 语法和空白检查 |

本地 `v6review-postgres-1` 容器状态为 healthy，未发布宿主机 5432 端口；本轮通过容器 bridge IP 配置 `V6_TEST_DATABASE_URL`，每个测试使用临时 schema 并在 teardown 删除，没有连接生产库。`tests/test_market_review_e2e.py` 仍因未设置 `V6_GOLD_SOURCE` skipped；不能把该项写成跨仓端到端通过。计划列出的 `scripts/check_worktrees.py` 在两个相关工作树中均未找到，因此不能伪造该脚本的运行结果。

提交前最终全量验证为 `PYTHONPATH=. .venv/bin/pytest tests -q`：1,595 passed、49 skipped、160 warnings、181 subtests passed，127.95s。此命令覆盖当前 HEAD 的全部收集用例；warnings 为现有 FastAPI 生命周期、依赖弃用和 pandas future warning，不含 test failure。

## 性能与证据边界

实现的接口上限是 realtime 2,000 条、15 分钟；索引 50 默认/200 最大；详情 10 分钟窗口、2,000 条图表点、200 条生命周期。合成 Chromium 已验证实时页没有 events/latest 请求、today 未选择详情请求数为 0、选择后为 1；页面隐藏停止轮询。

隔离库证据显示触发器投影是稳定成本，但不足以解释生产极值。生产只读样本（最近 20 个包）为每包 274 quotes、274 bands、957,607–959,779 JSON bytes；`market_review_current_observations` 仅 16 条存活行、456,080 条 dead tuples、总大小 1,532,370,944 bytes；`market_review_observations` 1,280,116 行、3,209,052,160 bytes；`market_review_snapshots` 27,564 行、1,212,579,840 bytes。该“重复 current UPSERT + 膨胀”是迁移级候选，不能在未获新迁移授权时改表或杀 idle transaction。

本地持续负载测试 `tests/test_market_ingest_load.py` 以 `V6_MARKET_LOAD_SECONDS=600` 完成：合成写入 599 次；login 120 次，耗时 p50 2.300ms、p95 3.781ms、max 5.975ms；realtime 读取 120 次，耗时 p50 3.447ms、p95 4.823ms、max 5.592ms。它是合成写入，不是生产登录或真实成交证据。

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_market_review_api.py tests/test_market_frontend.py tests/test_market_review_browser.py tests/test_market_split_browser.py tests/test_market_review_storage.py tests/test_market_review_e2e.py -q
```

生产仍没有读取账户、下单/撤单，未改 `execute=false`、密钥或数据库。缺市场专用登录浏览器，因此 Authelia 登录、callback、handoff、Bearer 动态 API 的完整浏览器链路暂未验收；匿名 302 与 backend 直达 302 不计为通过。

## 2026-09-10 热更新记录

用户随后明确授权直接热更新以查看效果。生产 txecs 的 `business-cn-public-web-1` 与 `business-cn-backend-api-1` 已用容器内临时文件加原子替换更新三页文件与后端文件；后端为单进程 uvicorn，无 reload 参数，因此仅重启该一个后端容器以重新导入 Python 模块。未运行迁移，未重启 public-web 或其他业务容器。

回读：本次 6 个运行文件 SHA-256 与本工作树一致：`page-common.js=92fdc49d…3de139`、`realtime.js=36f72c6…0c211`、`review-detail.js=fb04dea…b4c4f`、`market_snapshot.py=5f89b73…52218`、`market_review.py=78c564f…e9ee`、`main.py=8270ad5…6177a`。backend imports=ok、容器 healthy；backend OIDC login 连续 3 次均 302、0.002–0.041s；OIDC discovery 200、27.7ms；health 回读约 14–22ms；三页静态入口 200，769/938/1152 bytes，约 0.9–1.3ms；三个脚本 200，约 0.7–1.1ms。认证访问日志只保留 path，不保留 query。

动态市场 API 暂不列为完整通过：热更新后的生产日志仍出现约 15.1–15.2s 的 `statement_timeout` 回滚，以及有界容量下的 503；这证明认证事件循环不再被同步写入占住，但真实数据库写入成本仍未修复。之前的 90.96s/约 97s 长写入、无 blocker 的 IO/运行样本，以及约 449110s 的 `external_sources` idle transaction 均保留为诊断证据；没有终止该事务。下一步需要单独授权的新迁移，针对重复 current UPSERT/死元组做集合式投影或等价最小改写，并完成迁移前后兼容、回滚与持续写入验收。

跨仓重试边界：只读核对 Gold 原仓的 `snapshot_publisher.push_snapshot` 发现它对 HTTP 503 抛出 `RuntimeError`，本仓没有权限修改该原仓，且当前函数本身没有持久化快照 outbox。因此本次后端只保证“未 commit 不 ack、重复 run+sequence/event_id 幂等、满载显式 503 + Retry-After”；“生产发布器在 503 后一定不丢事件”仍未验证，不能写成已完成。后续需在 Gold 开发树得到明确授权后补持久 outbox/重试测试，再把该项从残留事项移除。

## 2026-09-11 详情超时修复

用户已实际完成登录并加载历史索引，但选择错单后浏览器显示“市场接口请求超时”。同一时段生产日志中详情路由实际返回 `200｜成功`，后端处理 7 次为 1,091–2,615ms；因此不是 OIDC、Nginx 504 或图表时间窗扫描未返回。对截图交易日最近事件做只读计数：1 个合约、10 分钟窗口 172 个图表点；事件 1,029 bytes、持仓投影 29,231 bytes、生命周期 14,777 bytes，而 172 个原样观察 JSON 合计 10,652,268 bytes，详情总响应 10,697,629 bytes。后端查询本身 595.67ms，但浏览器必须接收并解析约 10.7MB，超过页面 8 秒中止预算的风险由此形成。

`market_review.event_detail` 现在对持仓投影只返回页面需要的 `model/account/unresolved/reasons`，并把详情图表点投影为 `observed_at｜观测时刻`、`last_price｜成交价` 与 `center/lower/upper｜I 价格带`（可选 `fair_price｜公允价`）。完整 observations、position events 和 decisions 仍保留在数据库原始证据中，没有删除或改写；详情仍保留事件、最多 200 条生命周期、10 分钟窗口和最多 2,000 个图表点。所选 50 笔的最大持仓投影也从完整 138,076 bytes 缩为展示摘要 335 bytes。

本地新增回归用例先验证旧代码会泄露 `events/decisions/raw_snapshot` 而失败，再在修复后通过。执行 `PYTHONPATH=services/backend-api .venv/bin/pytest tests/test_market_review_api.py tests/test_market_frontend.py tests/test_market_split_browser.py tests/test_market_snapshot_contract.py tests/test_market_ingest_concurrency.py -q` 得到 24 passed、2 warnings、5.10s；`py_compile` 与 `git diff --check` 退出 0。

按已有直接热更新授权，仅更新 txecs `business-cn-backend-api-1:/app/app/market_review.py` 并重启该单后端容器；未运行迁移、未修改 Nginx、未触及 Gold 原仓或市场账户。生产回读 SHA-256=`537bbaa24fb6ecdb200e30e352242defd4c74384cedfb073a9209a8afee94560`，容器 `running/healthy`，`/healthz` 的数据库检查为 `ok`。以相同生产事件直读修复后的详情为 37,521 bytes（较 10,697,629 bytes 减少 99.649%）、172 个图表点、3 条生命周期、780.38ms；这条直读绕过 HTTP 认证，只证明详情构造与序列化边界。热更新后持续写入仍可见有界的市场写入 503（约 15.1s 事务预算）且动态 `/v1/market/latest` 681.93ms、`/v1/market/series` 40.39ms、`/v1/market/events` 36.94ms；这与已记录的写入瓶颈一致，不把它伪称为完全消失。

剩余验收：需要用户已登录市场浏览器再次选择历史/当日任意一笔，确认浏览器经过实际认证链路接收 37KB 级详情并呈现两张图表。该浏览器验证不能由无市场登录态的本机 Chrome 代替；用户本次“可登录、索引已加载”的反馈已经覆盖到 OIDC 返回与动态索引，但详情点击结果需在此热更新版本上复核。
