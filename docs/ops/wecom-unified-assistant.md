# 企业微信统一 AI 助手

## 边界

- OpenClaw 的官方 `@wecom/wecom-openclaw-plugin` 独占新机器人的 WebSocket。
- `openclaw-bridge` 负责企微 channel/lane、确定性命令短路及 WebDock 模型转发。
- backend-api 负责群绑定、消息/图片持久化和 AI 草稿状态；不直接调用企微 API。
- doc-sync-worker 继续负责把已确认节点写入「研发过程记录」。
- 旧 `WECOM_COMPANY_B_GROUPBOT_*` 监听在新机器人验收前保留；同一个 Bot ID 不得同时由两个进程建立 WebSocket。

## 用户命令

- 普通文字或图片：直接提问，走 WebDock/ChatGPT，不持久化普通问答图片。
- `#绑定 <审批单编号>`：确定性关联当前群，不调用模型。
- `#节点 <类型> <摘要>`：直接进入研发过程写表队列，不调用模型。
- 图片加 `#AI节点 <类型> <要求>`：调用一次图片模型并生成草稿，不立即写表。
- `#确认节点`：确认当前用户在本群最新的 AI 草稿，进入写表队列。
- `#取消节点`：取消当前用户在本群最新的 AI 草稿。

## 群聊交互与项目路由

- 群聊由企微平台在 `@统一 AI 助手` 时下发；未 @ 的普通群消息不会进入 OpenClaw，服务端无法补收。
- bridge 会在送入 ChatGPT 前移除开头的机器人 @，避免项目上下文出现无意义提及。
- 官方企微插件继续复用同一条流式消息：先显示“处理中”，再逐步更新并结束为最终答案。
- 企微A管理面板的 `企微AI助手配置` 是路由控制面。`配置编号 = *` 为默认项目；
  `group:<chatid>` / `user:<userid>` 可覆盖单个群聊或私聊，精确项优先。
- 当前默认项目为 `qwecom0`。在智能表修改后，经 doc-sync、backend 路由 JSON 和 WebDock 定时拉取自动生效。

## 安全与部署

- Bot ID/Secret 只在 infra 的 SOPS `openclaw.enc.env` 中保存；`openclaw.json` 仅保存 `${WECOM_UNIFIED_BOT_*}` 引用。
- bridge/backend 仅通过 ECS 环回地址通信，并使用 `OPENCLAW_INTERNAL_TOKEN` 鉴权。
- 节点图片写入 backend 与 doc-sync-worker 的 `wecom_group_media` 共享卷；数据库只保存受限路径。
- 先部署 AliECS/backend/bridge 与 WebDock lane，再启用企微插件；最后回归微信、飞书和企微。

## 验收

1. `openclaw channels status --deep` 显示 wecom 已配置且运行。
2. 企微私聊文字和图片均能得到回复。
3. 企微群 `#绑定`、`#节点` 不产生 WebDock 模型请求。
4. `#AI节点` 返回草稿；确认后 `group_messages.written_to_sheet=true`，智能表格出现对应行和图片。
5. WebDock archive 的 lane key 以 `wecom:` 开头，不与 `wechat:` / `feishu:` 混用。
6. 微信和飞书各发一条消息，均正常回复。
7. 群聊正文进入 archive 后不包含开头的 `@统一 AI 助手`，且未单独配置的会话进入 `qwecom0`。
