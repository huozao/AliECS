# 实时行情消息流实施计划（GPT-6 Sol 交接）

> 执行方式：单代理按任务顺序实施；使用 superpowers:executing-plans。用户要求的是可接续的设计计划，本文件不证明已部署消息流。不要因技能示例自动启动子代理。

**Goal:** 同一实时页支持本机直连与公网访问，完整保留 500 ms 展示桶，具备自动断线恢复、可解释状态与可测量延迟。

**Architecture:** Gold 输出统一展示记录，独立 WSL 发布服务维护当前状态、窗口快照与有界恢复记录；Centrifugo OSS 和官方 JS SDK 负责 WebSocket 订阅、连接维护与传输恢复。首期仅一个 WSL broker；本地网关直接访问，公网入口经已有私有通道转发到同一服务，保证两入口的数据身份一致。

**Tech Stack:** 现有 Python Gold、FastAPI 授权接口、Centrifugo OSS、官方 centrifuge-js、Lightweight Charts 5.0.8（先验证半秒时间轴）、现有 nginx 与私有转发通道。

**Spec:** 本文件第一至六节为设计契约，第七节为执行任务。所有新增文件名、接口名与容量值均为拟定值，P0 核实后记录最终值。

## 一、现状与证据边界

2026-09-28 本会话已检查的事实，接手后必须复核：

- 页面热修复提交 6923974、aee066a：首屏先 latest 再整窗，随后 since 增量轮询。尚未实现消息推送。
- AliECS 普通工作区存在其他 session 的修改，生产 backend 与该分支并非同一完整版本。禁止用旧本地 backend 直接重建覆盖生产。
- Gold Review API 的 source_revision 随快照 sequence/发布时间变化；把它每次回传会强制整窗重取。它不能作为新协议的 stream_epoch。
- 一次整窗与增量对比约为 5.985 秒/584942 bytes、0.565 秒/35750 bytes，仅为单次本地样本，不是 P95 或公网性能。
- 拆分查询的一次测量：latest 0.021 秒，曲线查询 1.034 秒，挂单历史查询 3.802 秒。不能只改传输协议后宣称首屏已解决。
- 当前 JS seriesPoints 把时间 Math.floor 到整秒，并反复 setData；历史 API bucket_seconds 最小为 1。
- 新发现：Gold market_review_runtime.py 的 observe 还使用 at.replace(microsecond=0) 构建 OHLC。当前展示层本身有 1 秒聚合；先核对采集、研究、展示三层桶，不能假定已有完整 0.5 秒展示桶。
- 纸面模型计算、模拟账户执行和页面传输分属不同验收范围。

## 二、成熟组件选型

采用 Centrifugo OSS + 官方 JS SDK 为默认方案，P0 做兼容性验证并锁定版本、镜像 digest、SDK checksum 与许可证。官方列出 VK、Badoo、ManyChat、OpenWeb、Grafana 等生产使用者；这是上游公开陈述，不是本项目性能保证。

复用连接心跳、订阅授权、传输序号、重连和短期消息历史，不自行开发完整 WebSocket broker。业务记录、窗口快照、策略状态、账户隔离仍由项目负责。

首期单个 WSL Centrifugo Memory Engine，明确接受 broker 重启后历史缓存丢失，依靠快照重建。暂不引入 Redis、Kafka 或第二个 broker；只有节点数量、内存或恢复需求的测量证明必要时再扩展。若所选 OSS 版本缺少必须能力或半秒实验失败，在 P0 写失败证据和替代比较，不静默退化到不可靠流。

参考（实施时核对已锁定版本的文档，不直接照搬最新版配置键）：

- https://centrifugal.dev/ ：上游生产应用声明。
- https://centrifugal.dev/docs/server/history_and_recovery ：epoch/offset、完整恢复与 recovered=false。
- https://centrifugal.dev/docs/server/authentication ：连接身份、有效期和 JWT。
- https://centrifugal.dev/docs/server/engines ：Memory/Redis 的持久性边界。
- https://grafana.com/docs/grafana/latest/setup-grafana/set-up-grafana-live/ ：单连接多订阅。
- https://tradingview.github.io/lightweight-charts/tutorials/demos/realtime-updates ：setData 首屏、update 增量。

## 三、拓扑、网址与职责

首期采用一个发布服务与一个 broker，避免本地、公网各维护一套行情状态。

```text
国内/国际源 -> Gold 计算 -> 有界 IPC -> WSL realtime_gateway -> Centrifugo
                             |              |                  |
                             |              +-- 窗口快照       +-- 本机网关 -> Windows 浏览器
                             +-- 原有归档                      +-- 私有通道 -> 公网 nginx -> 浏览器
```

- 本机：同一套静态页面通过可配置 localhost 端口访问；快照和 WebSocket 相对路径保持同源。
- 公网：保留现有市场域名与页面路径；拟新增 /api/v1/market/bootstrap、/api/v1/market/stream-token、/connection/websocket。
- 公网入口代理长连接到 WSL broker，不为每个客户端轮询 Gold，也不公开 broker 的管理和发布 API。
- 登录权限仍由 AliECS 管理；本地数据请求不经过公网，但首期登录/续期可能依赖中央认证。不要承诺完全离线登录。
- 本地会话引导使用短时一次性 handoff code，绑定浏览器 verifier、允许回调地址和一次兑换；不得把中央长期 Token 放 URL。实施前核对既有 handoff 代码能否复用。
- 业务 API 与 broker 只允许只读订阅；拒绝客户端发布、任意 channel 和越权账号 channel。
- 本地与公网连接的 broker epoch/offset 相同；将来增加节点时必须重新设计跨节点状态与恢复。
- 保留 HTTP 增量接口作为可见的降级模式；一旦降级标明刷新速度和数据新鲜度，不与 WebSocket 同时重复写页面状态。

## 四、业务消息与恢复契约

### 4.1 身份与字段

业务身份和传输身份必须分开：

- run_id：Gold 当前业务运行身份。
- stream_epoch：发布服务重建状态、无法证明连续性时变化；正常发一条消息不能改变它。
- source_sequence：同一业务 stream_epoch 内递增，发布重试沿用原值，不重复推进。
- broker epoch/offset：由 Centrifugo 管理，仅用于 broker 消息恢复；不是业务序号。
- schema_version、kind、bucket_start_ms、revision、final、source_time_ms、computed_at_ms、published_at_ms。
- quotes/bands 的键为 contract + bucket_start_ms；半秒桶必须保留毫秒精度。
- 对同一个键，只有更高 revision 可以替换；重复包幂等，旧 revision 不能覆盖新值。
- final 后的合法更正必须显式 correction，保留更正关系；不得伪造晚到数据为当时已知结果。
- paper 状态与 account 回报用独立且有权限边界的流标识；不把不同账户/run_id 事件合并。

JSON 批量消息优先，每次可包含多个合约的变化。500 ms 为展示桶，不是强迫上游源每半秒都有新成交。空桶是否存在及前值延续必须有 has_trade/source_age 标记；不能把前值延续计作新成交。模型价格带按真实模型更新时刻发布，不为凑半秒节拍重算策略。

### 4.2 快照与订阅原子衔接

1. 先订阅且缓冲消息，设置有限缓冲容量。
2. 从同一发布服务取原子快照：窗口 + run_id + stream_epoch + applied_source_sequence。
3. 安装快照，丢弃缓冲中不大于快照水位的重复包，再按连续序号应用后续包。
4. 同 epoch 且序号缺口仍在恢复缓存时自动补齐；缓存不足、epoch 改变或恢复失败，停止宣称连续并重新取快照。
5. 启动窗口选择、run 切换时带 generation；旧请求晚返回不得覆盖新状态。手动刷新也走同一流程。
6. broker recovered=true 只证明 broker 已接收消息的连续性。WSL 到 broker 发布失败必须由 source_sequence/发布健康单独发现。

发布服务应先串行应用状态与产生源序号，再由独立发送循环推送；发布超时可能已成功，重试允许重复，客户端幂等。快照水位表示已应用的业务状态，不谎称 broker 已确认。队列溢出不能静默丢包：使流失去连续性并发出 reset/resync，记录指标。慢订阅者不回压 Gold。

### 4.3 缓存、归档与业务事件

初始设计预算，均须配置化和压测：

- 展示快照环形缓存 20 分钟，覆盖 15 分钟最大选择；限制字节总量（起始 128 MiB，测后调整）。
- broker history TTL 120 秒、最多 10000 条；两限任一先到即失去相应恢复能力，不能按时间单独承诺。
- IPC 队列和客户端缓冲都有条数/字节双限。最大发送包建议先限 256 KiB，超过按协议拆分，不把业务事件截掉。
- 网关冷启动从既有持久证据恢复真实可用部分；原有 1 秒历史不可伪造为两个 0.5 秒桶。缺失区间明确标记。
- 最终桶和账户事件持久化保留独立路径；broker 短期缓存不成为审计账本。
- pending 桶的显示更新可以合并；账户事件与最终桶不能无声合并丢弃。不可恢复时明确展示缺口。

## 五、前端契约与半秒显示

- 保留现有国内合约布局、5/10/15 分钟选择、白色成交线、黄色中心和价格标签。
- 官方 SDK 管理连接；页面共享一个 socket。拆出 transport、state、chart 三个小模块，不重写整个市场站点。
- 数据 reducer 按消息序号应用全部有效变化；requestAnimationFrame 合并同一帧绘制，但不能因此丢最终桶或业务事件。
- 初始/重同步用 setData，普通新增或末桶更正用 update。窗口修剪低频批量进行，避免每条消息重建全部数组。
- 验证已安装 Lightweight Charts 对分数秒/custom horizontal scale 的支持；不允许 Math.floor 到秒或将两个半秒桶重用同一时间键。
- 源时间、展示桶时间和浏览器接收时间分别保留；价格线不得用网关接收时间重新定位。
- 连接状态、国内新鲜度、国际新鲜度、模型 readiness、账户执行许可独立字段。Unknown 不填 0，不以 socket 心跳判定行情新鲜。
- 认证到期触发刷新或重新登录，凭据撤销/权限变化应在明确的短时上界生效，不能让旧 socket 永久保留权限。

## 六、验收目标与证据

以下是设计目标，P0 可以用测量修订一次并注明理由，不是当前性能结论：

| 项目 | 验收目标 |
|---|---|
| 同机发布到浏览器状态应用 | P95 <= 200 ms，P99 <= 500 ms |
| 公网发布到浏览器状态应用 | P95 <= 500 ms，P99 <= 1000 ms；另报网络/时钟误差 |
| 正常半秒桶流 | 连续 30 分钟，所有实际发布的 final 桶最终可见；无静默缺口 |
| 热缓存首屏 | 当前卡片 P95 <= 500 ms；15 分钟窗口 P95 <= 2 秒 |
| 资源 | 1、5、20 客户端，记录 CPU/RSS/队列；Gold 迭代 P95 增幅目标 <= 10% |
| 恢复 | 断网 3 秒、30 秒、超过 TTL；全部正确补齐或明确重同步 |
| 安全 | 无权限、过期、错误 audience、任意 channel、客户端 publish 均拒绝 |

浏览器使用 performance.now 测本地耗时；跨机端到端延迟必须估计时钟偏移和误差范围，不能把未校时的 Date.now 差当作精确延迟。分列 source->compute、compute->publish、publish->receive、receive->state、state->render；状态应用和实际绘制不是同一个指标。

本地回放证明协议和负载；真实开市采集至少 30 分钟证明生产显示。二者均不能当作完整交易日模拟账户成交验收。休市时只报告链路/恢复测试通过。

证据保存在仓库外私有目录，按任务输出：baseline.json、versions.json、half-second.json、bootstrap-race.json、recovery.json、auth.json、latency-local.json、latency-public.json、load.json、production-readback.json、rollback.md。文档只记脱敏摘要。

## 七、任务顺序与文件边界

每项步骤：先写关键失败用例并确认失败，实现最小改动，跑相关检查，记录命令/退出码/证据，再做仅含本任务的本地 commit。以下新文件均为拟新增，不表示已经存在。

### P0：基线、依赖与半秒可行性（第一个执行任务）

- [ ] 读 Gold/AliECS/infra 各自 AGENTS、Gold CONSISTENCY 第 0 节、开发操作说明、AliECS fleet/deploy/project-ai-map；核对当前所有工作树、生产 SHA/文件 hash 和其他 session 的改动。
- [ ] 核对生产 backend 源码提交是否在本地；从匹配版本创建隔离分支，不从过时 main 重建。现有两次前端热修复先核对是否已被后续提交包含。
- [ ] 本地临时启动固定版本 Centrifugo + SDK，验证 Memory Engine 重连和重启失效；不接生产账户。
- [ ] 在真实浏览器运行 2 Hz、8 合约、15 分钟、1800 桶/合约的图表实验，验证 .000 与 .500 两点独立，断线恢复和更新无秒取整。
- [ ] 输出依赖版本、许可证/NOTICE、半秒支持路径和资源基线；P0 失败则解决具体阻碍再进入 P1。

预计文件：AliECS tests/test_market_stream_browser.py（新）、tests/fixtures/market-stream/（新）；临时实验资料不直接提交。

### P1：Gold 独立展示事件与发布边界

预计文件：Gold src/gold_spread_monitor/realtime_stream.py（新）、realtime_gateway.py（新）、market_review_runtime.py、monitor.py；tests/test_realtime_stream.py（新）。

- [ ] 先追踪真实 source timestamp 与当前 1 秒 OHLC 的使用者，保持研究/策略判据与现有归档兼容，新增 versioned 半秒展示投影。
- [ ] 非阻塞 IPC 交给独立服务，单 writer 产生 epoch/sequence，有限缓存与快照锁；禁止行情线程同步等待网络发布。
- [ ] 测试：源事件重复、乱序、半秒边界、无成交桶、晚到更正、队列满、网关退出、时钟回拨、国内休市但国际仍更新。
- [ ] 完成并测量内存快照；冷启动缺失数据明确 unavailable，不假造历史。

### P2：broker 发布、快照与恢复

预计文件：Gold realtime_gateway.py、realtime_stream.py、tools/check_realtime_stream.py（新）；AliECS local/market-stream/（新测试配置）。

- [ ] 固定版本的 publish API 客户端复用连接、有限重试；实现重试幂等与健康指标。
- [ ] 实现 bootstrap 水位与缓存消息拼接；定义 reset 和 gap 原因码。
- [ ] 故障测试：快照加载中持续发布、发布成功但 ACK 丢失、同 source_sequence 重投、broker 重启但 Gold 不重启、网关重启但 Gold run_id 不变、超过 history TTL。
- [ ] 验证恢复失败必然重同步，不能保留“连续”标签。

### P3：认证与两个同源入口

预计文件：AliECS services/backend-api/app/routers/market_stream.py（新）、app/main.py、tests/test_market_stream_auth.py（新）；Gold 本地网关认证适配。当前 app 包隔离约定必须沿用。

- [ ] 基于现有登录身份 + market.read 签发独立 audience、短有效期、限定 channel 的连接/订阅凭据。
- [ ] 实现本地一次性 handoff、同源会话、token 刷新；复用现有机制前验证其回调与 verifier 约束。
- [ ] 测未认证、权限撤销、重放 handoff、错误 Origin/audience、任意 channel、客户端 publish、账户 channel 越权。
- [ ] 私有发布密钥仅服务端持有；示例、测试与公开文档只用占位值。

### P4：页面消息流与图表增量

预计文件：AliECS services/public-web/market/realtime.js、realtime/index.html；realtime-stream.js、realtime-state.js（新）；tests/realtime-stream.test.cjs、tests/realtime-state.test.cjs（新）；沿用 tests/realtime-page.test.cjs。

- [ ] SDK vendoring/锁定，先订阅缓冲再 bootstrap；HTTP 降级有明确状态且单一 reducer 写入。
- [ ] 实现 sequence/revision 去重、半秒时间轴、update、低频窗口修剪；把状态卡接到服务端事实。
- [ ] 测 .000/.500 不合并、连接与报价新鲜度不同、run 切换、窗口切换晚响应、页面隐藏再显示、补发重复、旧 correction。
- [ ] 浏览器测 8 图实际渲染、内存稳定、最新跟随与用户查看历史互不抢动。

### P5：部署配置、转发与回滚

预计文件：infra 的市场 nginx/Compose 模板（先从 fleet/deploy 定位真实文件再填具体路径）；Gold scripts 与用户服务模板；AliECS docs/runbooks 下的现有市场说明。

- [ ] 固定镜像 digest；本地/public 两入口使用同版本静态资源。配置只给模板，不提交生产值。
- [ ] nginx 配置 WebSocket upgrade、超时与 Origin；仅暴露订阅端点。验证现有私有通道可承载长连接。
- [ ] 写出实际操作清单：文件备份与 hash、依赖启动顺序、开关、旧轮询回退、持久数据不受影响。
- [ ] 先隔离环境完成端到端测试。生产动作须按当前用户授权范围执行；没有跨会话明确发布权限时，准备可审阅产物后再申请。

### P6：生产实测与交接

- [ ] 回读两入口静态 hash、运行版本、broker 身份、run_id、源序号和账号边界。
- [ ] 执行第六节故障/延迟/负载矩阵，报告分位数、样本数、时钟误差与缺失分母。
- [ ] 开市真实数据 30 分钟观测；市场关闭则标 PARTIAL，给下一实际观测窗口。
- [ ] 输出已完成项、剩余断言、最小下一步；不得用测试绿、HTTP 200 或 socket connected 代替生产流验收。

## 八、执行命令模板与交接提示词

先找到项目现有 venv；隔离 worktree 不默认自带 venv。下列测试名中新增文件须实施后才存在。

```bash
# Gold：在自己的 Gold worktree
PYTHONPATH=src <gold-venv>/bin/python -m pytest -q tests/test_realtime_stream.py
python3 scripts/check_navigation.py
# AliECS：在自己的 AliECS worktree
node --test tests/realtime-stream.test.cjs tests/realtime-state.test.cjs tests/realtime-page.test.cjs
<aliecs-venv>/bin/python -m pytest -q tests/test_market_stream_auth.py tests/test_market_stream_browser.py tests/test_market_frontend.py
python3 scripts/check_navigation.py
# 使用隔离测试配置，禁止用生产 env 跑本地测试
```

复制给 GPT-6 Sol：

> 请阅读本计划并从 P0 开始按顺序实施。先审计当前分支/脏文件/服务版本和已有热修复，复用本任务隔离 worktree 或新建独立工作树；不得覆盖其他 session 的改动。先完成 Centrifugo OSS/SDK 固定版本、半秒图表和 snapshot/stream 衔接验证，再实现业务发布、授权、前端和转发。保持 execute=false、consensus.shadow_only=true，不触发真实或模拟账户报撤单。每项完成针对性测试并记录证据后继续下一项；本地回放、认证浏览器实测、真实开市数据分别报告。不要重复发起已确认设计的讨论；遇到实际架构阻碍时给出证据与最小修订。未经当前会话明确授权不 push/merge/部署或变更账户；先把待发布代码、配置模板、回滚步骤和测试证据做完整再提出具体发布动作。提交只包含本任务文件并附 Nav-Impact。不要把本计划中的设计目标写成已经通过的指标。

## 九、进度记录

- [x] 设计、上游资料核对与实施任务拆分完成。
- [ ] P0–P6 实施与验收尚未执行。
