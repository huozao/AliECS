# 统一消息中枢（notify）

所有出站通知的唯一出口：飞书、企微 A/B 群机器人、企微自建应用。
生产者默认只描述「发生了什么」；需要渠道特有排版时，可以在通用通知里显式提供
某个渠道的原生载荷。中枢仍负责鉴权、路由、幂等、重试和投递记账，不允许原生载荷
携带收件人、凭据或请求 URL。

## 按问题进入

| 任务 | 专项节 |
|---|---|
| 没收到、重复、重试 | [排障](#排障消息没收到)、[孤儿领养](#坑worker-写的行是孤儿必须由-flush-领养) |
| 新来源或字段 | [消息模型](#通用-notification-与渠道原生载荷新来源按这张表接入)、[新来源](#加一个新来源)、[跨镜像契约](#跨镜像契约) |
| 改卡片 | [通道能力](#三个通道的能力差异不要试图抹平)、[投递约束](#当前-json-20-卡片的投递约束)、[验证](#改了卡片渲染怎么验本地测试挡不住这类问题) |
| 流量日报/心跳 | 本文 aliecs、txecs 流量看护节 |
| bridge 运维告警 | [bridge 契约](#openclaw-bridge-运维告警) |

先读对应节；代码改动同时遵守 [通知开发约束](../development.md#通知)。本文维护当前契约，首次调用、旧部署版本及方案取舍原文移至 [治理历史归档](../migrations/2026-09-13-guidance-history.md)。运行位置与配置值从 [fleet](../fleet.md) 进入并只读核对，旧记录不能补全当前状态。

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

投递代码只有 backend-api 一份。HTTP 入口同步投递；worker 的 `_poll_once` 每轮调用 `/v1/internal/notify/flush` 带走积压，不能放在外层调度周期。正常路径的等待约为一个 poll 周期加上处理与渠道耗时；`DOC_SYNC_POLL_SECONDS` 是配置项，实际周期需读运行配置。历史耗时样本见归档，不构成时延上限。

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
| 标题背景色 | ✅ 12 色可选 | ❌ 只能给标题文字上色 | ❌ 同左 |
| 副标题 / 标题标签 | ✅ 标签最多 3 个 | ❌ 降级成标题下两行 | ❌ 同左 |
| 按钮 | ✅ 最多 5 个，9 种样式 | ❌ 降级成 markdown 链接行 | ❌ 同左 |
| 交互回调（点赞/确认） | ❌ 中枢没有入站链路 | ❌ | ❌ |

飞书那套上传移植自 `deploy/openclaw-bridge/openclaw_bridge.py`
（`feishu_upload_image` / `build_feishu_card`）。
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:feishu_upload_image -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:build_feishu_card -->
代码目标是让两条发送路径统一使用
JSON 2.0；卡片结构、图片字段和 Markdown 语义必须保持一致，避免同一条通知在中枢和
bridge 上出现不同渲染结果。

卡片使用 JSON 2.0；验收应覆盖发送 API 接受、真实客户端可见以及同 schema PATCH。查询原始卡片必须带 `card_msg_content_type=user_card_content`；未带该参数的兼容展示结构不能证明客户端渲染失败。

## 通用 Notification 与渠道原生载荷（新来源按这张表接入）

<!-- nav-check-python: services/backend-api/app/notify/models.py:Notification -->
`Notification` 是所有渠道共同的业务语义；`channel_payloads` 是一个**可选覆盖层**。
没有覆盖层时，所有渠道沿用下表的通用渲染；有覆盖层时，只有对应渠道使用原生载荷，
其他渠道仍按通用字段降级。因此生产者不需要为每个渠道复制一套发送逻辑。

```text
通用 Notification
        ├── 飞书：有 channel_payloads.feishu → 原生 msg_type/content 透传
        │          无该字段 → 中枢生成 JSON 2.0 卡片
        ├── 企微：始终使用通用字段降级渲染（markdown + 独立图片）
        └── 其他渠道：使用各自适配器
```

原生载荷的形状是：

```json
{
  "channel_payloads": {
    "feishu": {
      "msg_type": "interactive",
      "content": {"schema": "2.0", "config": {}, "header": {}, "body": {}}
    }
  }
}
```

`content` 可以是 JSON 对象/数组，也可以是已经序列化的 JSON 字符串；上限 256 KiB，
必须是合法 JSON。`msg_type` 只表示消息类型（例如 `interactive`、`text`、`post`、
`image`、`file`），收件人仍只能由中枢路由表提供。飞书原生投递失败时，适配器会回退
到通用 JSON 2.0 卡片，再失败才回退纯文本；原生载荷的拒绝原因只写适配器日志（不写入
正文），若通用降级也失败，才会由 `notify_deliveries` 记为 pending/dead。

下面是通用字段；不填就没有，全部有默认值，所以**老来源一个字都不用改**。

| 字段 | 类型 | 飞书渲染成 | 企微降级成 |
|---|---|---|---|
| `title` | str | 卡片标题（自带图标就不叠级别图标） | markdown 首行加粗 |
| `subtitle` | str | 标题下的副标题 | 标题下一行 |
| `theme` | 枚举 | 标题条背景色；**留空则按 `level` 取蓝/黄/红** | 无（只有字色） |
| `tags` | ≤3 个 | 标题右侧彩色标签 | 标题下一行的 `` `行内代码` `` |
| `summary` | str | 正文首段（markdown） | 第二行 |
| `segments[].text` | str | markdown 段；`preformatted=true` 走纯文本段 | 原样一行 |
| `segments[].fields` | ≤40 | 2.0 `column_set` 两列 | `**名**：值` 逐行 |
| `segments[].image` | ≤12 张 | 2.0 `img`，`caption` 成图题 | 另发独立图片消息 |
| `buttons` | ≤5 | 2.0 `button` / `column_set` | 每个一行 markdown 链接 |
| `link` | 兼容字段 | 折算成第一个按钮 | 同上 |

`channel_payloads` 不参与企微渲染，企微只读取 `title`、`summary`、`segments`、
`buttons` 等通用字段。这保证同一条通知同时投递到飞书和企微时，飞书可以使用自己
的 JSON 2.0 版式，而企微仍获得完整且可读的降级正文。

### 当前出站来源清单（2026-09-12）

| 来源 | 事件范围 | 上行路径 | 飞书原生载荷 | 说明 |
|---|---|---|---|---|
| `gold-spread-monitor` | `data_backfill_complete`、纸面成交/对冲/平仓 | notify-center HTTP | 补数卡片按需透传；其余通用渲染 | 研究与模拟交易通知 |
| `quota-monitor` | `quota.*` | notify-center HTTP | 通用字段 | AI 额度日报与状态 |
| `feishu-obsidian` | 快照/同步结果 | notify-center HTTP | 通用字段 | 文档快照同步 |
| `doc-sync` | 文档同步/心跳 | worker outbox | 通用字段 | 服务端内部生产者 |
| `devbox-traffic` | `traffic.*`、`governance.daily` | notify-center HTTP | 日报透传 JSON 2.0；启动/阈值与引导体检用通用字段 | devbox 流量采集器与日报；工作区 AI 引导每日体检（AliECS-WebDock `scripts/governance_daily.py`，路由 id=20 `governance.*`） |
| `txecs-traffic`、`aliecs-traffic`、`txecs-disk` | 流量/磁盘看护 | notify-center HTTP | 通用字段 | 主机看护 |
| `phonedock` | `attendance.*` | notify-center HTTP（webdock2 本机 18020） | 通用字段 | 海康互联考勤定时查看（周一至周六两次）；失败时 `attendance.check_failed`+截图；源码 `huozao/phonedock` |
| `sub2api` | `sub2api.deploy.*`、`sub2api.backup.*`、`sub2api.notify.test` | notify-center HTTP（webdock2 WSL 经 `172.17.0.1:18020`） | 通用字段 | 私有 Sub2API 部署/备份失败（同因只报一次、24 小时提醒）与恢复；路由 id=23 → devbox 运维群；生产者 infra `roles/webdock/extras/sub2api/notify.py`（2026-10-24） |

OpenClaw 的 `/im/v1/messages` 调用仍保留在**会话传输适配器**内，用于回复指定
飞书消息、上传附件和 PATCH 已发送卡片；它们没有广播通知的 `source/event/dedup`
语义，不能误列为通知生产者。OpenClaw 运维告警只走 `notify-center`；已入队消息由中枢重试，入队前失败只记日志，详见本文 bridge 契约。

### aliecs 流量日报卡片（2026-09-05 已确认）

日报不是普通 `fields` 两列，而是 `section` 段落；飞书使用 JSON 2.0 的加权
`column_set`，每个区块固定为「标题 / 指标名 / 指标值」三列，当前权重为 `4 / 3 / 5`。
标题使用 `normal`，日期与指标名使用 `notation`，指标值使用 `normal`，并且不在行内添加
彩色圆点，以避免移动端自动换行。

日报生产者位于 `infra/roles/server/aliecs-traffic/files/traffic-guard.py`：

- 「昨日流量」来自 `vnstat --json d` 的上一自然日；
- 「昨日 sing-box」来自脚本按轮次差值封存的 `previous_day_total`，不可用时显示「未计到」；
- 月底外推按当月实际天数计算；
- `txecs：心跳用于判断失联/限速。` 是日报尾部说明；日报缺席仍是失联判据。

真实客户端验证过的预览链：`preview4`（两列）、`preview5`（三等宽失败）、
`preview7`（JSON 2.0 加权列）、`preview10`（当前紧凑字号版本）。本次已在当前接收群
完成肉眼验收并正式部署；后续调整仍须先发真实卡片再部署，不要回退到 JSON 1.0，也不要把
三列改成等宽 `div.fields`。

### 主题和按钮样式

`theme` 取值：`blue` `wathet` `turquoise` `green` `yellow` `orange` `red` `carmine`
`violet` `purple` `indigo` `grey` `default`。

`buttons[].style` 取值与**实测观感**（别照名字猜）：

| style | 实际长相 |
|---|---|
| `default` | 白底黑字灰边框 |
| `primary` | 白底蓝字蓝边框——**是描边不是实心** |
| `primary_filled` | 蓝底白字实心，视觉最重的主按钮 |
| `danger` / `danger_filled` | 同上，红色 |
| `text` / `primary_text` / `danger_text` | 无边框纯文字 |
| `laser` | 渐变强调 |

### 当前 JSON 2.0 卡片的投递约束

被拒的后果是 `feishu.send` **静默降级成纯文本**，群里只会看到卡片变成了一段字，
不报错。所以这三条都在 `Notification` 入口就校验，不等投递：

1. `theme` / `tags[].color` / `buttons[].style` 必须在白名单内。
2. `tags` 最多 3 个，第 4 个是**拒绝**不是截断。
3. 图片使用 2.0 的 `scale_type: fit_horizontal`，不要与 `size` 同时出现；结构变更必须
   同时做 API 接收检查和真客户端肉眼验收。

### 做不到的（别答应业务）

- **交互回调按钮**（点赞、点踩、确认、屏蔽）：需要公网入站端点 + 飞书验签 + 在开放
  平台配「消息卡片请求网址」。中枢现在是纯出站管道（`notify_outbox → notify_deliveries`），
  没有反方向的链路。
- **复制 / 朗读按钮**：飞书 AI 回复气泡上那三个是**客户端原生 UI**，不是卡片组件，
  机器人发的卡片渲染不出来。

### 改了卡片渲染怎么验（本地测试挡不住这类问题）

本地单测只证明结构与逻辑；发布验收还需渠道与真实客户端证据。以下调用和真实发消息均须在用户授权范围内进行，尚未授权时先完成本地校验并明确待验项：

1. **零噪音结构校验**：在卡片末尾追加一个必错哨兵元素再 POST。飞书只报第一个错误，
   所以报错路径指向哨兵 = 前面全部合法，而整张卡片仍被拒、群里不留消息。
   拿它可以批量试枚举值，也可以拿生产 `notify_outbox` 里的**真实 payload** 全量回归。
2. **发一条真卡片**肉眼看排版。合成样本全绿不代表真样本能过。

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

<!-- nav-check-python: services/doc-sync-worker/app/notify_client.py:request_flush -->
`worker_loop.py` 里 `request_flush()` 必须放在 **`_poll_once` 内**（约 30 秒一次，
`DOC_SYNC_POLL_SECONDS`），不能放在外层 `while True` 开头——外层一轮 = 一个完整调度
周期（`interval_seconds` 默认 86400），放那里等于**一天才冲刷一次**。

同一天踩的第二个坑：第一次修完孤儿领养后，通知写进去 120 秒仍未被带走，就是这个。
`tests/test_doc_sync_worker.py::test_notify_flush_runs_once_per_poll_not_once_per_cycle`
守着它，判据是「flush 次数 == poll 次数」而不是「flush 次数 > 0」——后者在错误层级下
照样成立。

<!-- nav-check-python: services/backend-api/app/notify/models.py:NotifyImage -->
## 已知限制

- **重试出去的消息没有图**。`payload_json` 刻意不存 base64——一张 PNG 是几十万字符，
  会把这一列撑爆（`gold_spread_alerts` 的 `_strip_chart_bytes` 已经踩过）。所以首次
  投递有图，进了重试队列之后按无图纯文本降级：字还在，图没了。
- 企微应用投递依赖正确的应用身份和 SOPS 渲染配置；缺少 agentid 不能推断没有应用，按本文「企微应用身份」核对。
- 重试退避 `[60, 300, 1800, 7200]` 秒，四档全部用完判 `dead`，不再重试。
  退避索引和最终 dead 状态由 `tests/test_notify_center.py` 的重试用例覆盖，必须验证各档实际秒数及耗尽后的行为。
- worker 的 `send_feishu_text`（`app/pipelines/sync_alert_notifier.py`）与 `send_feishu_alert`（`app/pipelines/tplus_parent_match.py`）返回值只表示写入中枢队列是否成功；实际投递看 `notify_deliveries`。不要套用到 bridge 的同名函数。
  <!-- nav-check-python: services/doc-sync-worker/app/pipelines/sync_alert_notifier.py:send_feishu_text -->
  <!-- nav-check-python: services/doc-sync-worker/app/pipelines/tplus_parent_match.py:send_feishu_alert -->
- 图片只收 PNG，2MB 上限，在 `NotifyImage` 入口就校验（收敛前这层在 gold_spread 的
  `_upload_charts` 里）。2MB 取的是三家里最紧的企微群机器人限制，省得同一条消息
  发得出飞书发不出企微。
- **卡片被拒的具体原因，2026-09-04 之前在日志里读不到**。飞书用 HTTP 400 返回，
  把「哪个元素哪个字段不合法」写在响应体里，而 `urlopen` 在读响应体之前就抛
  `HTTPError`——原来的 `_post_json` 没接这个异常，那行信息整个丢掉，线上只剩
  「卡片怎么变成纯文本了」一个现象。现已把响应体前 400 字带进异常消息。

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

## 响应、日志与凭据

重复提交返回既有 outbox 的当前投递汇总（sent/pending/dead）；duplicate 不等于未投递。200 仍须区分无路由、成功和重复提交，不能直接视为送达。

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

容器重建、日志轮转或清理可能丢失旧日志；部署前先采集所需证据。单纯 restart 不应被当成必然清空日志。

### 读 notify_outbox 时 id 会缺号，这是正常的

<!-- nav-check-python: services/backend-api/app/notify/store.py:enqueue -->
`enqueue` 用 `INSERT … ON CONFLICT (dedup_key) DO NOTHING`，而 Postgres 的序列
**即使没有插入也会消耗掉一个值**。所以重复提交（生产者重试、网络重发）会在
`notify_outbox.id` 里留下空洞。看到 id 不连续不要当成「有行被删了」去查。

### 企微应用身份

使用规范环境变量 `WECOM_COMPANY_A_AGENT_ID` / `WECOM_COMPANY_B_AGENT_ID`；历史 typo 键仅用于兼容回落。应用 ID 必须与 token 所属应用匹配，不能只验证变量能被读取。真实值从 SOPS 渲染并在授权验证时核对，历史归档里的值不能当作当前配置。

### 标题图标不叠加

生产者自己写的标题图标（gold-spread-monitor 每一类都带：✅ ⛔ 🔴 🟢 🧾 🧪 ℹ️ ⚠️）
优先于中枢的级别图标——它往往更具体（🧾 收盘复盘说的是「哪一类」不是「多严重」）。
<!-- nav-check-python: services/backend-api/app/notify/models.py:display_title -->
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

### 心跳验收

先只读核对接收端实际开关、最近设备心跳、来源及路由；不能用代码默认值或历史上线记录证明看护生效。缺席场景应在隔离测试环境验证；生产造故障、停 timer 或改业务时间戳另需明确授权。

## txecs 流量看护：同群但独立来源（2026-09-11）

`txecs-traffic` 与 `aliecs-traffic` 使用同一个飞书群，但保持独立的 source、周期账本、
`dedup_key` 和日报心跳。txecs 账本按腾讯云流量包的明确重置时间计算，不使用 vnstat 的
自然月汇总；设备侧按 `eth0` TX 采集，初始账单基线与云控制台对账后再校准。

两台设备的日报不能合并成一条：任一设备出网故障时，另一台仍须独立报警，且接收端可以
按 source 判断哪一台的心跳缺失。需要减少群消息时，只能在中枢侧另加总览摘要，不能替代
两条设备侧日报和跨档告警。

### 账单与阈值验收

除日报投递凭证外，跨档告警应在真实跨档或隔离测试中验证；每个流量包周期结束后，将设备 TX 与云账单对账。某次日报成功不能证明所有档位或后续周期都正确。

### 生产者 schema

卡片 fields 使用对象 `{"name": "指标", "value": "值"}`，不能传二元数组。修改设备生产者 schema 后，除脚本测试外还须用 backend-api 消息模型解析一次，再在授权范围内验证真实投递；422 的非法消息补发需重建原 dedup key 并检查实际 deliveries。

### 上线时踩到的三个「只有真实调用才会显形」的问题

1. **`doc-sync` 的既有路由 `event_pattern` 是 `sync_alert`，不是 `*`。** 心跳告警的 event 是
   `aliecs.traffic.heartbeat_missing`，**匹配不到任何路由**，会变成无路由墓碑行、永远发不出去。
   所以单独加了一条 `doc-sync` / `aliecs.traffic.*` / `warn` 的路由。
   **加新 event 前先查这张表，别假设某个 source 的路由是通配的。**
2. **设备先装好、`notify_sources` 行后插，中间窗口内投递全是 401**，而设备侧当时把整个 4xx
   当成「不可重试」直接丢弃——第一条日报就这么没了。设备侧已改成只丢 400/422，
   但**上线顺序仍应是「先插 SQL 建好来源和路由，再装设备侧」**，别依赖重试兜底。
3. **postgres 容器的用户和库都是 `app`，不是 `postgres`。** 用 `psql -U postgres` 会得到
   `FATAL: role "postgres" does not exist`，看起来像库坏了。正确写法：

   ```bash
   ssh txecs "sudo docker exec business-cn-postgres-1 psql -U app -d app -c '<SQL>'"
   ```

   （`notify_deliveries` 没有 `updated_at` 列，写查询时别顺手加。）

### 建这条通道的三条 SQL

**是三条不是两条**——第三条容易漏，漏了心跳告警发不出去（见上文第 1 点）。

```sql
-- 1. 来源（库里只存 sha256；token_digest 就是 sha256(utf-8 明文) 的 hex）
INSERT INTO notify_sources (source_key, token_sha256, note)
VALUES ('aliecs-traffic', encode(sha256('<明文token>'::bytea), 'hex'),
        'aliecs 出方向流量看护；token 在 infra secrets/aliecs-traffic.enc.env');

-- 2. 设备侧的告警与日报
INSERT INTO notify_routes (source_key, event_pattern, min_level, channel, target_json, note)
VALUES ('aliecs-traffic', '*', 'info', 'feishu',
        '{"profile":"COMPANY_A","receive_id":"<运维群 chat_id>","receive_id_type":"chat_id"}',
        'min_level 取 info 是为了日报也进群——只有告警没有基线，涨到一半也看不出异常');

-- 3. 接收端的心跳缺席告警。它用 source='doc-sync' 写回（不能用被监视的
--    source_key，否则会把心跳「续上」自己消掉触发条件），而 doc-sync 的既有路由
--    只匹配 event_pattern='sync_alert'，不加这条就永远匹配不到任何路由。
INSERT INTO notify_routes (source_key, event_pattern, min_level, channel, target_json, note)
VALUES ('doc-sync', 'aliecs.traffic.*', 'warn', 'feishu',
        '{"profile":"COMPANY_A","receive_id":"<运维群 chat_id>","receive_id_type":"chat_id"}',
        '心跳缺席告警');
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

已接入来源的跨服务契约（事件名、dedup_key 规则、触发口径）各有一份，改那条通道前先读：

| 来源 | 契约 |
|---|---|
| `quota-monitor` | [`docs/notify/quota-monitor.md`](../notify/quota-monitor.md) |
| `feishu-obsidian` | [`docs/notify/feishu-obsidian.md`](../notify/feishu-obsidian.md) |

消息本身能填哪些字段、飞书能渲染成什么样、哪些写法会让卡片被拒——见
[通用消息模型](#通用-notification-与渠道原生载荷新来源按这张表接入)。新来源不需要了解飞书卡片 JSON，只填那张表里的字段。

## 跨镜像契约

`services/doc-sync-worker/app/notify_client.py` 构造的 payload 必须能被
`services/backend-api/app/notify/models.py` 的 `Notification` 解析。两边在不同镜像里，
**没有编译期检查**，由 `tests/test_notify_center.py::CrossServiceContractTests` 守着。
<!-- nav-check-python: tests/test_notify_center.py:CrossServiceContractTests -->
改任一侧的 payload 结构都要跑那个测试——否则 worker 写进去的消息会在投递侧解析失败，
而且失败是异步的、当场看不出来。

### openclaw-bridge 运维告警

bridge 运维告警使用来源 `openclaw-bridge`、事件 `bridge.alert`，只走中枢 `POST /v1/internal/notify/send`；会话回复、附件和卡片 PATCH 仍属于会话传输。

`send_feishu_alert()` 在中枢不可用时只记失败日志，不直发飞书。HTTP 502 且响应表明 outbox 已建立时，中枢负责重试；传输失败或配置错误不能当作已入队，也没有 bridge 本地持久补发队列保证。排障必须分别确认请求是否进入中枢和投递状态。
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:send_feishu_alert -->

新部署先登记来源及路由，再从 SOPS 渲染 `NOTIFY_CENTER_ENDPOINT`、`NOTIFY_CENTER_SOURCE`、`NOTIFY_CENTER_TOKEN`、`NOTIFY_CENTER_TIMEOUT_SECONDS`，最后按授权范围发布。endpoint 随网络模式核对，运行位置只查 [fleet](../fleet.md) 与 [deploy](deploy.md)。SQL 中使用目标占位符，不在文档复制真实群 ID。

验证覆盖成功投递、502 已入队和中枢不可用三类；代码测试见 `tests/test_openclaw_bridge.py`，真实通知验证须有发送授权。
