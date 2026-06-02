# 集成 Webhook 网关设计

## 目标边界

AliECS 作为公网 webhook 入口，负责接收畅捷通、企业微信、飞书等外部平台事件，并在后续版本中承担验签、解密、token/ticket 管理、事件存储和后台核验入口。

`services/tplus-sync-worker` 只负责 T+ OpenAPI 的主动数据同步。它不暴露公网入口，不负责 APP_TICKET 接收，也不长期保存真实密钥。

## 当前初版范围

- `backend-api` 预留三个占位入口：
  - `POST /v1/webhooks/chanjet`
  - `POST /v1/webhooks/wecom`
  - `POST /v1/webhooks/feishu`
- 当前响应只确认收到请求，不做验签、解密、落库或业务处理。
- `services/tplus-sync-worker` 保持只读同步能力：读取环境变量、调用 T+ OpenAPI、分页同步 BOM、保存原始 JSON、导出 Excel。
- `db/migrations/0007_integration_gateway.sql` 只预留事件和 token 表，不接入当前占位路由写入链路。

## 职责拆分

| 模块 | 职责 | 不负责 |
|---|---|---|
| `backend-api` webhook gateway | 公网入口、验签解密、APP_TICKET/openToken 管理、事件存储、后台查询 | 执行 T+ 批量同步 |
| `tplus-sync-worker` | T+ OpenAPI 只读同步、原始响应归档、Excel 导出 | 公网 webhook、验签解密、长期 token 管理 |
| `doc-sync-worker` | 企业微信/飞书文档类数据同步 | Webhook 网关入口 |

## 平台差异

### 畅捷通

- 关键后续能力：APP_TICKET 接收、AES 解密、openToken 换取和刷新。
- `openToken` 不建议长期手动写死；后续应由 `backend-api` 接收 APP_TICKET 后换取并安全保存。
- T+ 数据同步仍由 `tplus-sync-worker` 主动执行，避免公网回调直接触发重任务。

### 企业微信

- 需要区分通讯录、应用回调、文档/智能表格相关事件。
- 回调验签解密和 access_token 管理应在 `backend-api` 中统一处理。
- 智能表格全量/手动同步继续遵守 `doc-sync-worker` 边界。

### 飞书

- 需要处理 challenge 校验、事件订阅、tenant access token/app access token。
- 后续若接入飞书表格同步，仍建议走独立 worker，不让 webhook 请求直接跑长任务。

## 环境变量设计

示例配置只能使用占位值，不写真实 AppKey、AppSecret、openToken、certificate、appTicket。

| 变量 | 归属 | 用途 |
|---|---|---|
| `CHANJET_APP_KEY` | backend-api / tplus-sync-worker | 畅捷通应用标识 |
| `CHANJET_APP_SECRET` | backend-api / tplus-sync-worker | 畅捷通应用密钥 |
| `CHANJET_OPEN_TOKEN` | tplus-sync-worker 初版 | 本地只读同步临时使用，后续应由 token 服务托管 |
| `CHANJET_WEBHOOK_AES_KEY` | backend-api 后续 | 畅捷通回调解密 |
| `CHANJET_WEBHOOK_TOKEN` | backend-api 后续 | 畅捷通回调验签 |
| `WECOM_WEBHOOK_TOKEN` | backend-api 后续 | 企业微信回调验签 |
| `WECOM_WEBHOOK_AES_KEY` | backend-api 后续 | 企业微信回调解密 |
| `FEISHU_VERIFICATION_TOKEN` | backend-api 后续 | 飞书事件校验 |
| `FEISHU_ENCRYPT_KEY` | backend-api 后续 | 飞书事件解密 |

## 数据库预留

`integration_events` 用于保存外部事件原始内容、处理状态和错误信息。

`integration_tokens` 用于保存平台 token 元信息。真实 token 值后续必须加密后保存，不能明文落库。

## 安全规则

1. 当前仓库只保存 `.env.example` 占位值。
2. 不提交真实 AppKey、AppSecret、openToken、certificate、appTicket 或 access token。
3. webhook 日志只允许输出 provider、事件类型、状态码、event_id 等可排查字段。
4. 原始事件落库前应先确认不会包含不必要的敏感内容；必要时拆分 masked 字段。
5. webhook 接收请求只做快速处理，长任务通过队列、sync_requests 或 worker 命令异步执行。

## 下一步建议

1. 先实现畅捷通回调验签和 AES 解密测试。
2. 再实现 APP_TICKET 事件入库和 openToken 换取服务。
3. 最后让 `tplus-sync-worker` 从安全 token 来源读取 openToken，替代手工 `.env` 固定值。

## 2026-06 Chanjet first implementation

- Public message URL: `https://hydwang.xyz/api/v1/webhooks/chanjet`.
- Public OAuth callback URL: `https://hydwang.xyz/api/v1/webhooks/chanjet/oauth`.
- The Chanjet POST webhook now accepts encrypted `encryptMsg` payloads, decrypts them with `CHANJET_WEBHOOK_AES_KEY`, parses `APP_TICKET` / `TEMP_AUTH_CODE` style messages, and returns `{"result":"success"}` quickly.
- Decoded Chanjet events are spooled to `CHANJET_EVENT_SPOOL_DIR` for the first integration pass. This directory is runtime-only and may contain sensitive ticket/code material, so it must not be copied into Git, chat, logs, or public artifacts.
- The token helper supports both documented paths:
  - app-settled path: `appTicket -> appAccessToken -> tempAuthCode -> permanentAuthCode -> orgAccessToken -> user authorization token`.
  - self-built path: `appTicket + certificate -> accessToken`.
- `tplus-sync-worker` still reads `CHANJET_OPEN_TOKEN` from its ignored `.env` until token storage is moved behind `backend-api`.

## 2026-06 T+ long-running worker

- Production Compose now includes `tplus-sync-worker` as a long-running service.
- The first sync starts immediately after container start, then repeats by `TPLUS_SYNC_INTERVAL_SECONDS`.
- Output is persisted through Docker volumes at `/app/data` and `/app/output`.
- The first safe production scope is the verified BOM read-only `QueryPage` sync. Additional Chanjet/T+ modules should be added only after their official read-only Query/QueryPage endpoints are confirmed and covered by tests.
