# quota-monitor → 统一消息中枢契约

本文是 WebDock2 `quota-monitor` 接入 AliECS 统一消息中枢的跨服务契约。quota-monitor
只负责采集、判断和提交事件，不构造飞书卡片；卡片渲染、图片上传、重试和投递记账由
`backend-api/app/notify` 负责。

## 来源与路由

实现前由运维在 SOPS/数据库中注册一个独立来源（不要复用已有来源 token）：

```text
source_key: quota-monitor
event_pattern: quota.*
min_level: info
channel: feishu
target_json: {"profile":"COMPANY_A","receive_id":"oc_84d1130542509e374f7ea20c13d11ca4","receive_id_type":"chat_id"}
```

`target_json` 只保存 profile、收件人和类型，飞书 app secret 仍由 SOPS 渲染到
backend-api 环境变量。路由变更后必须用真实测试事件验证 `notify_deliveries`，不能仅看
HTTP 200。

## HTTP 提交

```http
POST /v1/internal/notify/send
X-Notify-Source: quota-monitor
X-Notify-Token: <source token>
Content-Type: application/json
```

请求体必须符合 `app.notify.models.Notification`。额度日报示例（字段值均为示例）：

```json
{
  "source": "quota-monitor",
  "event": "quota.daily_report",
  "level": "info",
  "title": "AI 额度日报",
  "subtitle": "WebDock2 · 2026-09-06 08:00 (Asia/Singapore)",
  "theme": "blue",
  "tags": [{"text": "Codex", "color": "blue"}, {"text": "Claude", "color": "violet"}],
  "summary": "两个平台均已完成采集。",
  "segments": [
    {"kind": "fields", "fields": [
      {"name": "Codex 5h", "value": "剩余 82% · 01:42 后重置"},
      {"name": "Claude 7d", "value": "剩余 64% · 周一 09:00 重置"}
    ]},
    {"kind": "image", "image_ref": "codex-latest"},
    {"kind": "image", "image_ref": "claude-latest"}
  ],
  "images": [
    {"ref": "codex-latest", "caption": "Codex 页面截图", "png_base64": "<PNG base64>"},
    {"ref": "claude-latest", "caption": "Claude 页面截图", "png_base64": "<PNG base64>"}
  ],
  "link": {"text": "查看历史截图", "url": "https://hydwang.xyz/console/quota/"},
  "dedup_key": "quota:daily_report:2026-09-06:morning",
  "occurred_at": "2026-09-06T00:00:00Z"
}
```

重置事件使用稳定的自然主键，确保同一窗口只通知一次：

```text
quota:reset:<provider>:<account_alias>:<window_type>:<reset_at_epoch>
```

推荐事件名：

| 事件 | level | dedup_key 规则 |
|---|---|---|
| `quota.daily_report` | `info` | `quota:daily_report:<date>:<morning\|noon\|evening>` |
| `quota.reset` | `warn` | `quota:weekly_reset:<provider>:<weekly_reset_at>`（仅周额度） |
| `quota.auth_required` | `error` | `quota:auth:<provider>:<date>` |
| `quota.stale` | `warn` | `quota:stale:<provider>:<date>:<reason>` |
| `quota.schema_changed` | `error` | `quota:schema:<provider>:<date>:<fingerprint>` |

## 图片与重试边界

`NotifyImage` 入口会检查 PNG、base64 和 2 MiB 大小上限；飞书首次投递时由 channel 上传
`im/v1/images`，卡片使用 JSON 2.0 的 `img` 元素和 `scale_type: fit_horizontal`。

当前 `notify_outbox.payload_json` 不保存图片字节，异步重试会按纯文本降级。这是现有中枢
为避免数据库被截图撑爆而设的明确行为。quota-monitor 必须把截图同时保存到自己的历史
存储；若首投失败且业务要求重试仍带图，应在 quota-monitor 侧重新提交一个带新截图的
事件（使用新的尝试标识），或后续单独扩展媒体引用表，不能假定 `flush` 会恢复图片。

## 调度与确认

- 采集刷新：20–30 分钟有界随机间隔，仅用于控制轮询负载；不自动登录或执行账户操作。
- 定时报表：由 quota-monitor 以 `Asia/Singapore` 运行，每日早/中/晚各一次。
- 重置告警：**仅周额度**在两次连续健康采集间发生恢复或重置时提交 `quota.reset`；
  5 小时窗口重置不告警。事件使用 `quota:weekly_reset:<provider>:<weekly_reset_at>` 去重。
- 每次提交记录返回的 `outbox_id`，并查询
  `GET /v1/internal/notify/deliveries/{outbox_id}`；`notify_deliveries.sent` 才算实际送达。
