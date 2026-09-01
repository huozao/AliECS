# 统一消息中枢（notify）

所有出站通知的唯一出口：飞书、企微 A/B 群机器人、企微自建应用。
生产者只描述「发生了什么」，渲染成各家原生格式是中枢的事。

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

## 数据流

```
任意通用脚本 ── POST /v1/internal/notify/send ───────────────────────┐
gold-spread-monitor ── POST /v1/internal/gold-spread/alerts ── 业务适配 ┤
backend-api 内部生产者 ────────────────────────────────────────────────┤
                                                                    ▼
                                                           dispatch.deliver()
                                                                    │
doc-sync-worker ── notify_client.enqueue() ───────────────────────────┤
                                                                    ▼
                                                 notify_outbox（唯一汇聚点，dedup_key 幂等）
                                                                    │ 路由匹配 notify_routes
                                                                    ▼
                                                 notify_deliveries（每 target 一行）
                                                                    │
                                                     feishu / wecom_bot / wecom_app
```

**汇聚点是数据库不是 HTTP**。doc-sync-worker 与 backend-api 是两个镜像、构建上下文
互不可见（CI 的 context 是各自的 `services/<name>`），走 HTTP 会给 worker 平添一个
「backend-api 必须活着」的依赖。两者连同一个库，所以都只做「往 outbox 写一行」。

投递代码只有 backend-api 一份。触发两条：HTTP 入口同步投递；worker 的 `_poll_once`
每轮调一次 `/v1/internal/notify/flush` 带走积压（**不是外层 while True**，理由见下文
「冲刷频率跟 poll 走」）。所以 worker 写的通知最长延迟一个轮询周期
（`DOC_SYNC_POLL_SECONDS`，默认 30s）——2026-08-31 上线实测 **34 秒**：
写入 → 领养 → 投递到飞书。

留下这个数字是为了以后判断得出「慢了」。只写「最长一个轮询周期」的话，
延迟涨到几分钟也看不出异常。

## 四张表

| 表 | 作用 | 关键判据 |
|---|---|---|
| `notify_sources` | 上行鉴权，一来源一 token（只存 sha256） | `enabled=false` 即刻吊销该来源 |
| `notify_routes` | (source, event, level) → channel + target | `source_key` / `event_pattern` 支持 glob |
| `notify_outbox` | 唯一汇聚点 | `dedup_key` 唯一索引 = 幂等闸门 |
| `notify_deliveries` | 每 target 一行 | **「发出去没有」只看这张表，不看 outbox** |

路由表只存凭据的**引用名**（profile / env 变量名），密钥仍旧只在 SOPS。

## 三个通道的能力差异（不要试图抹平）

| | 飞书 | 企微群机器人 | 企微自建应用 |
|---|---|---|---|
| 一条通知落成几条消息 | 1（卡片装下全部） | 1 + 每张图各一条 | 1 + 每张图各一条 |
| 图片 | `im/v1/images` → image_key | base64 + md5，**2MB 上限** | `media/upload` → media_id |
| 正文上限 | 宽松 | markdown **4096 字节** | markdown 4096 字节 |
| 收件人 | chat_id / open_id | 固定群，改不了 | 人 / 部门 / 标签 |
| 需要的凭据 | app_id + secret | 只要 webhook URL | corpid + secret + agentid |

飞书那套上传与卡片结构移植自 `deploy/openclaw-bridge/openclaw_bridge.py`
（`feishu_upload_image` / `build_feishu_card` / `_lark_md`），那份在飞书链路上已经
跑了几个月，富文本、图片、文件都验证过。

## 排障：消息没收到

按这个顺序查，**不要从「是不是 API 挂了」开始**：

```sql
-- 1. 消息进来了没有？没有 = 生产者没发出来或 token 不对（看 backend-api 日志 401）
SELECT id, source_key, event, level, created_at FROM notify_outbox
ORDER BY id DESC LIMIT 10;

-- 2. 有没有路由命中？targets=0 是最常见的「没收到」原因，
--    而且这种情况 /send 返回的是 delivered:false 而不是报错
SELECT * FROM notify_deliveries WHERE outbox_id = <上一步的 id>;

-- 3. 投递失败在哪一步
SELECT id, channel, status, attempts, last_error, next_attempt_at
FROM notify_deliveries WHERE status IN ('pending','dead') ORDER BY id DESC LIMIT 20;
```

⚠️ **`/send` 返回 200 不等于对方收到了**。返回值要同时读 `delivered` 和投递计数：

- `delivered: true` — 至少一个 target 发成功
- `delivered: false, reason: "no matching route"` — 落库了，但没有任何路由命中，**没人会收到**
- `duplicate: true, delivered: true, sent > 0` — 这个 `dedup_key` 以前已经投递成功，本次不重复发送
- `duplicate: true, delivered: false, pending > 0` — 以前已入队但仍在重试，不能当成收到
- `duplicate: true, delivered: false, targets: 0` — 以前已入队但没有路由，仍然没人会收到

首次提交时全部 target 都失败会返回 502（消息已落库，会重试）；重复提交读取上面的当前状态。

每次提交都会返回 `outbox_id`。需要确认异步重试后的真实结果时，使用同一来源凭据查询：

```http
GET /v1/internal/notify/deliveries/<outbox_id>
X-Notify-Source: <source_key>
X-Notify-Token: <source_token>
```

响应只给该来源自己的 `sent / pending / dead` 计数及各 channel 状态，不返回
`target_json`、收件人或任何凭据引用；查询别的来源统一返回 404。

## 坑：worker 写的行是「孤儿」，必须由 flush 领养

worker 只写 `notify_outbox`，**不建 `notify_deliveries`**——它不读路由表，也不该读
（路由是投递侧的事）。所以 worker 写的行落库时没有任何投递记录，必须由 flush
领养：匹配路由 → 建投递记录 → 投递。

2026-08-31 上线自检时踩到：少了领养这一步，worker 写的通知会安全落库然后
**永远发不出去，且三处观测面全都显示「正常」**——

| 观测点 | 现象 | 看起来 |
|---|---|---|
| `notify_outbox` | 有行 | 消息收到了 ✅ |
| `notify_deliveries` | 没行 | 没有失败记录 ✅ |
| `flush` 返回 | `claimed: 0` | 没有积压 ✅ |

三个都是「正常」的样子，合起来才是「消息丢了」。判据是那条
**「有 outbox 行但没有 deliveries 行」**的 SQL：

```sql
SELECT o.id, o.dedup_key, o.source_key, o.event
FROM notify_outbox o LEFT JOIN notify_deliveries d ON d.outbox_id = o.id
WHERE d.id IS NULL;
```

正常情况下这个查询应当只在「刚写入、还没 flush」的瞬间返回行。持续有行 =
flush 没在跑（worker 主循环挂了，或 `NOTIFY_FLUSH_TOKEN` / `NOTIFY_FLUSH_URL` 不对）。

⚠️ **上面这句话在 2026-08-31 之前会误导**：那时「一条路由都没命中」的 outbox 行
（`delivered:false / no matching route`）**永远**没有投递记录，于是永久满足这个查询，
flush 每轮（约 30 秒）把它重新领养一次、建不出记录、再记一条 `matched no route`，
无限空转——而判据会告诉你「flush 没在跑」，方向正好相反。2026-08-31 端到端验证
`/v1/internal/notify/send` 时实测到（当时的 outbox 7）。

现在这种行会被写一条**墓碑投递记录**（`route_id=NULL`、`channel='none'`、
`status='dead'`、`last_error='no matching route'`），所以：

- 上面那条孤儿查询恢复成可信判据；
- 「为什么没发出去」终于出现在 runbook 让你查的那张表里，而不是只能靠读
  `/send` 的返回值——**「没配路由」和「还没投递」不再长得一样**。

`flush` 的返回里 `adopted` 是本轮领养的孤儿数，`claimed` 是重投的失败记录数，两者独立。

### 冲刷频率跟 poll 走，不跟 cycle 走

`worker_loop.py` 里 `request_flush()` 必须放在 **`_poll_once` 内**（约 30 秒一次，
`DOC_SYNC_POLL_SECONDS`），不能放在外层 `while True` 开头——外层一轮 = 一个完整调度
周期（`interval_seconds` 默认 86400），放那里等于**一天才冲刷一次**。

同一天踩的第二个坑：第一次修完孤儿领养后，通知写进去 120 秒仍未被带走，就是这个。
`tests/test_doc_sync_worker.py::test_notify_flush_runs_once_per_poll_not_once_per_cycle`
守着它，判据是「flush 次数 == poll 次数」而不是「flush 次数 > 0」——后者在错误层级下
照样成立。

## 已知限制

- **重试出去的消息没有图**。`payload_json` 刻意不存 base64——一张 PNG 是几十万字符，
  会把这一列撑爆（`gold_spread_alerts` 的 `_strip_chart_bytes` 已经踩过）。所以首次
  投递有图，进了重试队列之后按无图纯文本降级：字还在，图没了。
- **企微 B 没有自建应用 agentid**。SOPS 里 B 只有 `WECOM_COMPANY_B_GROUPBOT_ID/SECRET`
  和 `APP_SECRET`，没有 agentid，所以 B 目前只能走群机器人。要用应用消息得先在企微
  后台建应用并把 agentid 进 SOPS。

  ⚠️ **「B 没有自建应用」这个说法自 2026-08-31 起确认不准确**：B 有自建应用
  （`agentid=1000003`，名称 `AGI-达`），只是 agentid 没进 SOPS，容器里
  `WECOM_COMPANY_B_gentId` 是空字符串。用 B 的 `APP_SECRET` 调
  `agent/list` 就能读到。缺的是配置，不是应用。
- 重试退避 `[60, 300, 1800, 7200]` 秒，四档全部用完判 `dead`，不再重试。
  ⚠️ **2026-08-31 之前实际不是这个序列**：`mark_failed` 取的是
  `BACKOFF_SECONDS[attempts]`，而 `attempts` 在取值前已自增，于是第一次失败等的是
  **300 秒**而不是 60，首档 60 永远取不到，实际序列是 `300 → 1800 → 7200 → dead`。
  生产实测过（2026-08-31 outbox 10：失败 02:49:30 → 重投 02:54:40，间隔 310 秒）。
  已改成 `BACKOFF_SECONDS[attempts - 1]` 并把 `MAX_ATTEMPTS` 提到 `len+1`，
  由 `tests/test_notify_center.py::RetryAccountingTests::test_backoff_uses_every_tier_starting_at_the_first`
  守着——**那个测试断言的是具体秒数**，原来的测试只断言 `status=='pending'`，
  在正确和错误索引下都成立，所以它没挡住。
- **`send_feishu_text` / `send_feishu_alert` 的返回值语义变了**。收敛前 `False` 是
  「飞书没收到」，收敛后是「没能写进 outbox」。写进去之后投递成没成，只有
  `notify_deliveries` 知道——调用点如果拿这个返回值做业务判断，要重新审一遍。
- 图片只收 PNG，2MB 上限，在 `NotifyImage` 入口就校验（收敛前这层在 gold_spread 的
  `_upload_charts` 里）。2MB 取的是三家里最紧的企微群机器人限制，省得同一条消息
  发得出飞书发不出企微。

## 上线步骤

```bash
# 1. 迁移（0052 建四张表，不写任何数据）
# 2. 部署 backend-api + doc-sync-worker（compose 已加 NOTIFY_* 与三家凭据）
# 3. 把四处旧告警的收件人搬进路由表——值从既有 env 读，收件人不变
python3 scripts/seed_notify_routes.py            # 预演
python3 scripts/seed_notify_routes.py --apply    # 写入
```

`NOTIFY_FLUSH_TOKEN` 是新增的，需要先进 SOPS（`sops set infra/secrets/txecs-production.enc.env`）。
没有它的话 worker 的 flush 调不通——通知仍会落库，但要等到有 HTTP 请求进来才被带走。

### gold-spread-monitor 的业务适配边界

⚠️ 「gold-spread-monitor 不需要改，schema 和发送确认语义全不动」自 2026-08-31 起确认失效。
该说法会让复盘进度继续只传一段重复的 `summary`，而且生产者只看 `ok=true`，无法证明
`notify_deliveries` 里是否真的有成功记录。新的边界如下：

- 它仍调用 `POST /v1/internal/gold-spread/alerts`，保留黄金价差业务校验、
  `gold_spread_alerts` 业务表和断网时的本机磁盘队列。
- `replay_summary` 可带结构化的分区进度、并行进程、已运行时间、预计剩余时间、
  最新产物时间和带分母的正式报告指标；业务适配器把这些字段转换为中枢 `segments`。
- 端点返回中枢的 `outbox_id / targets / sent / pending / dead / duplicate`；生产者只有在
  已送达或确认是已送达事件的重复提交时才清除本机队列。
- 飞书/企微卡片、图片上传、收件人路由、幂等、重试和投递记账仍然只在 `app/notify/`；
  业务适配器不直接调用任何 IM API，因此没有第二套消息平台。

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

### 422 在服务端留痕（2026-08-31 起）

422 的详情原来只回给调用方，服务端日志里只有一行 `status_code: 422`，定位靠猜。
现在 `app/main.py` 有 `RequestValidationError` 处理器，会记下**出错的字段路径**：

```bash
ssh txecs "sudo docker logs business-cn-backend-api-1 2>&1 | grep 'request validation failed' | tail -3"
```

```json
{"message":"request validation failed","path":"/v1/internal/gold-spread/alerts","status_code":422,
 "fields":[{"type":"literal_error","loc":"body.kind"},{"type":"missing","loc":"body.occurred_at"}]}
```

⚠️ **只记 `type` 和 `loc`，故意不记 `input`/`msg`**——那两个会把请求体的值抄进日志，
而这个处理器对全站所有端点生效（登录、密钥、回调都经过它）。改动前先想清楚这一点。

⚠️ **`docker logs` 会在容器重启时清空**。要留证据就在部署前先捞出来。

**待办（截至 2026-08-31 未完成）**：gold-spread-monitor 的 422 还没抓到真实样本——
部署（17:03 CST）之后一直没有新的 gold 请求。已知的形状：
`monitor` 每轮可能出现 3×422 + 200，**但不是每轮都有**（11:14 CST 那轮有、
15:37 CST 那轮只有 200），所以它是间歇的、跟 payload 种类相关。
根因机制已定位一半：`gold-spread-monitor` 的 `alerts.py::_try_post` 用
`for attempt in range(3)` 把 422 当可重试错误重试 3 次——4xx 是确定性错误，
重试必然全失败，只是把一次失败放大成三条。**缺的是「哪个字段」**，等上面那条日志。

### 读 notify_outbox 时 id 会缺号，这是正常的

`enqueue` 用 `INSERT … ON CONFLICT (dedup_key) DO NOTHING`，而 Postgres 的序列
**即使没有插入也会消耗掉一个值**。所以重复提交（生产者重试、网络重发）会在
`notify_outbox.id` 里留下空洞。看到 id 不连续不要当成「有行被删了」去查。

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

### 标题图标不叠加

生产者自己写的标题图标（gold-spread-monitor 每一类都带：✅ ⛔ 🔴 🟢 🧾 🧪 ℹ️ ⚠️）
优先于中枢的级别图标——它往往更具体（🧾 收盘复盘说的是「哪一类」不是「多严重」）。
判断只有 `Notification.display_title()` 一处，飞书卡片头、企微 markdown 首行、
纯文本兜底三处共用；各写一套迟早会出现两边标题不一致。

⚠️ 判据不能只看 Unicode 类别：`ℹ`（U+2139）的类别是 **Ll（小写字母）**而不是符号，
只按类别判会漏掉 `ℹ️ ` 和 `⚠️ ` 开头的标题。真正要问的是「是不是按 emoji 呈现」，
那由后面的变体选择符 `U+FE0F` 决定，两个条件都要看。

## aliecs 流量看护：一条「靠缺席报警」的通道（2026-09-01）

`aliecs-traffic` 是第一个**设备侧**来源，走 `POST /v1/internal/notify/send`。它发两种：
跨档告警（50/100/150/180/200 GB，级别 info→fatal）和每天一条日报。

**日报不是可有可无的例行公事，它是心跳。** 理由是这条链路有一个别的来源都没有的性质：

> aliecs 按使用流量计费，越过 200 GB 闸门后公网被限速到约 3.4 KB/s
> （2026-08-15 实测）。**那时它自己的告警 POST 也发不出去。**
> 设备侧再怎么重试、落盘补发，都补不上「已经打不出去了」这一种。

所以配套装了一条**反向看护**在 worker 侧：
`services/doc-sync-worker/app/pipelines/heartbeat_watch.py`，挂在 `_poll_once`
（不是外层 `while True`，理由同下文〈冲刷频率跟 poll 走〉），自带每小时节流，查
`notify_outbox` 里 `source_key='aliecs-traffic'` 的最新一行超过阈值就报。

三个容易写错的地方，都有测试守着（`tests/test_doc_sync_worker.py::AliecsHeartbeatWatchTests`）：

1. **告警必须用别的 source_key 写回 outbox**（现在是 `doc-sync`）。用被监视的那个，
   这条告警行自己就把心跳「续上」了，下一轮发现「最近有行」于是不再告警——
   自己消掉自己的触发条件，而 outbox 有行、deliveries 有行、看护也在跑，三处全绿。
2. **默认关闭**（`ALIECS_TRAFFIC_HEARTBEAT_MAX_AGE_HOURS` 不设或 ≤0）。采集器还没装就打开
   会天天报「心跳缺失」，而那只是「还没上线」。**打开这个开关的前提是已经收到过第一条。**
3. 阈值建议 **30 小时**，容忍错过一次日报（日报按设备本地日期发，不是固定时刻）。

设备侧的实现、档位依据与 vnstat 对账要求在 infra `roles/server/aliecs-traffic/README.md`。

### 建这条通道的两条 SQL

```sql
INSERT INTO notify_sources (source_key, token_sha256, note)
VALUES ('aliecs-traffic', encode(sha256('<明文token>'::bytea), 'hex'),
        'aliecs 出方向流量看护；token 在 infra secrets/aliecs-traffic.enc.env');

INSERT INTO notify_routes (source_key, event_pattern, min_level, channel, target_json, note)
VALUES ('aliecs-traffic', '*', 'info', 'feishu',
        '{"profile":"COMPANY_A","receive_id":"<运维群 chat_id>","receive_id_type":"chat_id"}',
        'min_level 取 info 是为了日报也进群——只有告警没有基线，涨到一半也看不出异常');
```

`min_level` 若改成 `warn`，日报不再进群但**仍会写进 outbox**（无路由时写墓碑行），
心跳看护照常工作——它查的是 outbox 不是 deliveries。

## 加一个新来源

```sql
-- token 明文只在这一刻存在，记下来给生产者，库里只留 sha256
INSERT INTO notify_sources (source_key, token_sha256, note)
VALUES ('my-device', encode(sha256('<明文token>'::bytea), 'hex'), '说明');

INSERT INTO notify_routes (source_key, event_pattern, min_level, channel, target_json, note)
VALUES ('my-device', '*', 'warn', 'feishu',
        '{"profile":"COMPANY_A","receive_id":"oc_xxx","receive_id_type":"chat_id"}', '说明');
```

改完路由不需要重启：`matching_routes` 每次投递都读表。

## 跨镜像契约

`services/doc-sync-worker/app/notify_client.py` 构造的 payload 必须能被
`services/backend-api/app/notify/models.py` 的 `Notification` 解析。两边在不同镜像里，
**没有编译期检查**，由 `tests/test_notify_center.py::CrossServiceContractTests` 守着。
改任一侧的 payload 结构都要跑那个测试——否则 worker 写进去的消息会在投递侧解析失败，
而且失败是异步的、当场看不出来。
