# 飞书 ChatGPT 会话管理台上线验证记录

日期：2026-06-18

## 已完成

- AliECS 本地实现：飞书「会话索引表」可同步到 `managed_contacts`，并驱动 `/v1/routing/feishu-projects.json`。
- WebDock 热更新：优先使用真实 ChatGPT widget 截图，避免克隆渲染丢失深色背景里的白色文本。
- 生产热修：向 `managed_contacts` 临时写入 `hao (Lark)` 的 Feishu 路由，恢复 `/新对话` 到 ChatGPT 项目首页的导航来源。新规范下私聊路由 key 应为 `user:ou_28d4...`。
- 旧电脑 WebDock 已重建并运行健康。

## 关键验证

- ECS backend-api：`http://127.0.0.1:8000/healthz` 返回 OK。
- ECS Feishu 路由：`/v1/routing/feishu-projects.json` 应返回 `user:ou_28d4... -> https://chatgpt.com/g/g-p-6a2ffe0bac248191988612d9081dd6b1-lark-hao/project`。旧热种子 `ou_28d4...` 仅作为过渡兼容。
- WebDock 拉取结果：`/var/lib/webdock/browser_data/feishu_projects.json` 已包含同一条 Feishu 路由。
- `/新对话` 合成验证：`om_codex_newchat_003` 返回新会话确认；随后 `om_codex_newchat_004` 进入新 conversation `.../c/6a338e4e-1e60-83e8-8307-83f2f8c997f6` 并返回测试 token。
- OpenClaw bridge：`/v1/models` OK，OpenClaw gateway `/healthz` OK。

## 剩余阻塞

- 生产 `FEISHU_*` Bitable 环境变量未配置，`doc-sync-worker sync-feishu-full` 不能真实读取飞书多维表格。因此当前 Feishu 路由依赖手动热种子。正式自动化可以改为用自建应用权限自动创建/发现多维表格，把 `app_token/table_id/view_id` 存数据库；但自建应用凭据仍需要安全来源。
- 当前 OpenClaw bridge 收到的 Feishu 元数据只有 `channel/chat_type/peer_id/message_id`，未看到 `mentions` 或原始群事件。群消息「全量记录但仅 @ 机器人回复」不能靠文本猜测上线，应接入原始 Feishu webhook，或让 OpenClaw 明确转发 `mentions` 后再启用门控。

## 后续建议

1. 在生产 `release-meta.env` 中补齐 Feishu Bitable 配置。
2. 正式发布 AliECS doc-sync-worker 镜像后运行 `sync-feishu-full`。
3. 接入 Feishu 原始消息 webhook，落库消息日志/回复任务，再启用群 @ 门控。
