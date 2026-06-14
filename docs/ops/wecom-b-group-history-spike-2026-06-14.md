# 企微B 群历史消息可行性 spike（2026-06-14）

## 结论

当前企微B智能机器人/长连接 SDK 不能拉取“群里所有历史聊天记录”。

可做的只有两类：

- 从机器人接入后开始，通过长连接/Webhook 接收机器人可见的新消息，并前向留存。
- 如需历史与全量合规存档，必须走企业微信「会话内容存档」能力，单独开通、配置 RSA 公钥/私钥、使用会话存档 SDK 按 seq 增量拉取并解密。

## 核实依据

- 企业微信智能机器人长连接能力是 WebSocket 实时推送模型：创建 API 模式机器人、获取 Bot ID/Secret、连接后接收用户消息，未描述“拉取历史群消息”接口。参考：
  - https://developer.work.weixin.qq.com/document/path/101463
  - https://www.codebuddy.cn/docs/cli/wecom-bot-setup
  - https://docs.langbot.app/zh/usage/platforms/wecom/wecombot
- 社区实现和文档样例显示长连接收到的是 `aibot_msg_callback`，字段包括 `msgid/aibotid/chatid/chattype/from/msgtype/text`，属于新消息推送模型。
- 会话内容存档是另一套能力：使用企业微信会话存档 SDK，按 `seq` 和 `limit` 拉取密文消息，再用私钥解密；不是智能机器人 Bot Secret 的能力。参考：
  - https://developer.work.weixin.qq.com/document/path/91360
  - https://zhuanlan.zhihu.com/p/597147920
- 会话内容存档的群信息接口也要求使用“会话内容存档应用 secret”获取 access token；这与智能机器人 Bot ID/Secret 不同。参考：
  - https://open.work.weixin.qq.com/api/doc/90000/92951

## 已实现的前向捕获

新增表：

- `wecom_b_messages`

新增 endpoint：

```text
POST /v1/webhooks/wecom-b/messages
```

支持企微智能机器人长连接/回调形状：

```json
{
  "cmd": "aibot_msg_callback",
  "body": {
    "msgid": "MSGID",
    "aibotid": "BOTID",
    "chatid": "CHATID",
    "chattype": "group",
    "from": {"userid": "USERID"},
    "msgtype": "text",
    "text": {"content": "@机器人 你好"}
  }
}
```

落库字段：

- `msg_id`
- `bot_id`
- `chat_id`
- `chat_type`
- `sender_id`
- `msg_type`
- `content`
- `raw_json`
- `received_at`

如配置 `WECOM_B_CAPTURE_TOKEN`，调用方必须带请求头：

```text
X-Wecom-Capture-Token: <token>
```

## 已跑离线验证

```powershell
$env:PYTHONPATH='.'; pytest tests/test_wecom_b_capture.py -v
```

结果：`1 passed`。

## ⚠️ 人工/ops 步骤

1. 确认企微B智能机器人配置为长连接或 URL 回调模式，并能把 `aibot_msg_callback` 转发到 backend。
2. 若公网回调，配置反代到：

```text
POST https://hydwang.xyz/api/v1/webhooks/wecom-b/messages
```

3. 配置 `WECOM_B_CAPTURE_TOKEN` 并在转发端带 `X-Wecom-Capture-Token`。
4. 发一条新群消息，确认 `wecom_b_messages` 有新增记录。
5. 若必须拿历史消息：管理员开通企业微信「会话内容存档」，完成合规授权、公私钥和 SDK 部署后，再做独立 ETL；这不属于智能机器人 Bot 能力。
