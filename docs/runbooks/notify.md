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
外部设备(gold-spread-monitor / 任意脚本)
    │  POST /v1/internal/notify/send
    │  X-Notify-Source + X-Notify-Token
    ▼
backend-api 内部生产者 ──── dispatch.deliver() ────┐
doc-sync-worker 内部生产者 ── notify_client.enqueue() ┤
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

投递代码只有 backend-api 一份。触发两条：HTTP 入口同步投递；worker 主循环每轮调一次
`/v1/internal/notify/flush` 带走积压。所以 worker 写的通知最长延迟一个轮询周期
（`DOC_SYNC_POLL_SECONDS`，默认 30s）。

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

⚠️ **`/send` 返回 200 不等于对方收到了**。三种 200 要分开读：

- `delivered: true` — 至少一个 target 发成功
- `delivered: false, reason: "no matching route"` — 落库了，但没有任何路由命中，**没人会收到**
- `duplicate: true` — 这个 dedup_key 之前已经进过队，本次不投递

全部 target 都失败时返回 502（消息已落库，会重试）。

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
- 重试退避 `[60, 300, 1800, 7200]` 秒，四次用完判 `dead`，不再重试。
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

### gold-spread-monitor 不需要改

它发的还是 `POST /v1/internal/gold-spread/alerts`，schema、token、本地磁盘队列全不动。
那个端点仍旧存在（它有业务 schema 和 `gold_spread_alerts` 业务表），只是内部把
「自己拼卡片、自己传图、自己调 im/v1/messages」换成了 `deliver_alert()`。
少改一个跑在 Windows 上的独立仓，就少一处回归面。

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
