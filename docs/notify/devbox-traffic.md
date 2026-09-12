# devbox-traffic → 统一消息中枢

devbox 的代理流量采集器和日报不再持有飞书 app 凭据，也不直接调用
`open.feishu.cn/im/v1/messages`。两个 PowerShell 入口通过
`infra/secrets/devbox-traffic.enc.env` 解密三项运行参数：

```text
NOTIFY_ENDPOINT
NOTIFY_SOURCE=devbox-traffic
NOTIFY_TOKEN
```

上行请求使用 `X-Notify-Source` / `X-Notify-Token`，正文是通用
`Notification`。日报同时提供 `channel_payloads.feishu` 的 JSON 2.0
interactive 卡片；启动和阈值告警只提供通用文本，企微等渠道可以直接降级渲染。

中枢迁移由 `0063_devbox_traffic_notify.sql` 注册来源和路由。路由目标继续是迁移前
的运维群，目标标识只在 `notify_routes.target_json` 中维护，脚本不保存收件人。

验收不能只看 HTTP 200：发送后必须按返回的 `outbox_id` 查询
`GET /v1/internal/notify/deliveries/{outbox_id}`，确认对应 `channel=feishu` 的
`status=sent`。如果以后增加企微路由，同一条通用通知无需修改生产者。
