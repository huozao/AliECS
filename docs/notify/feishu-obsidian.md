# feishu-obsidian → 统一消息中枢契约

本文是 devbox 上飞书快照拉取（`huozao/Feishu-Obsidian` 仓）接入统一消息中枢的跨服务契约。
生产者只提交事件，不构造飞书卡片；渲染、重试和投递记账由 `backend-api/app/notify` 负责。

## 为什么发送点在 devbox 而不是 webdock2

链路是两段：webdock2 每日 03:0x 采集飞书并导出增量快照，devbox 每日 04:00 拉取快照落到
`C:\data\feishu-obsidian\vault`。通知发在**拉取侧**——那是链路终点，一处发送不会同一批文章报两次；
webdock2 采集失败的表现是拉不到新快照，按「没有新内容」的口径合并上报，不单独区分采集失败与拉取失败。

devbox 在容器外、也不在 txecs 本机，所以走 `ssh txecs` 调回环端点，不经公网：

```text
ssh txecs → http://127.0.0.1:8000/v1/internal/notify/send
```

token 经 stdin 传给远端 shell 变量，不出现在 txecs 的 `ps` 里。

## 来源与路由（2026-09-07 已注册）

```text
source_key:    feishu-obsidian
event_pattern: feishu-obsidian.*
min_level:     info
channel:       feishu
target_json:   {"profile":"COMPANY_A","receive_id":"oc_84d1130542509e374f7ea20c13d11ca4","receive_id_type":"chat_id"}
```

`min_level` 取 `info` 是因为「有新文章」本身就是 `info`：设成 `warn` 会把日常通知全滤掉，
只剩告警，等于没有基线。token 在 `infra secrets/feishu-obsidian-devbox.enc.env`，库里只存 sha256。

## 事件与触发口径

| 事件 | level | 触发 | dedup_key |
|---|---|---|---|
| `feishu-obsidian.new_articles` | info | 本轮有新文章，每次都报，带篇数与标题 | `feishu-obsidian:new:<date>:<count>` |
| `feishu-obsidian.idle` | warn | 拉取成功但连续 48 小时没有新文章，之后每 48 小时最多再报一次 | `feishu-obsidian:idle:<date>` |
| `feishu-obsidian.pull_failed` | warn | 连续失败满 48 小时才报，同样 48 小时一次 | `feishu-obsidian:pull_failed:<date>` |

**「有没有新文章」的判据落在 `00_干净原文库/**.md` 有没有新增**，不是 manifest 的文件总数——
`90_系统记录/` 和 `02_所有文章目录/` 每轮都在变，用总数会让「没有新内容」永远触发不了。
状态在 `C:\data\feishu-obsidian\.sync\notify-state.json`，判定逻辑是纯函数
`scripts/notify_pull_result.py::decide`，由该仓 `tests/test_notify_pull_result.py` 守着阈值。

生产者侧两条已经踩过的坑，改这条通道时不要退回去：

- **HTTP 200 不算送达**。中枢在没有路由命中时回 `delivered:false`，脚本按失败处理。
- **通知发不出去时不记录本次告警时间**，让下一轮重新判定；记下去会「以为报过了」而静默 48 小时。

## 接入验证记录（2026-09-07）

真实发送一条 `new_articles`（标题前缀 `[接入验证]`）：`/send` 返回
`delivered:true, targets:1, sent:1`，`notify_deliveries` 中 `outbox_id=1488` 的记录为
`channel=feishu, status=sent, attempts=1`。判据取的是投递记录，不是 HTTP 状态码。
