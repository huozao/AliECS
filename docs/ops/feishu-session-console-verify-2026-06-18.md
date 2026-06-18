# 飞书 ChatGPT 会话管理台上线验证记录

日期：2026-06-18

## 已完成

- AliECS 本地实现：飞书「会话索引表」可同步到 `managed_contacts`，并驱动 `/v1/routing/feishu-projects.json`。
- WebDock 热更新：优先使用真实 ChatGPT widget 截图，避免克隆渲染丢失深色背景里的白色文本。
- 生产热修：向 `managed_contacts` 临时写入 `hao (Lark)` 的 Feishu 路由，恢复 `/新对话` 到 ChatGPT 项目首页的导航来源。新规范下私聊路由 key 应为 `user:ou_28d4...`。
- 旧电脑 WebDock 已重建并运行健康。
- AliECS 本地新增：`doc-sync-worker` 可用飞书自建应用权限创建「飞书 ChatGPT 会话管理台」和 6 张数据表，并把 `app_token/table_id` 登记到 `external_sources`；后续同步可直接复用数据库记录，不再要求表级环境变量常驻。

## 关键验证

- ECS backend-api：`http://127.0.0.1:8000/healthz` 返回 OK。
- ECS Feishu 路由：`/v1/routing/feishu-projects.json` 应返回 `user:ou_28d4... -> https://chatgpt.com/g/g-p-6a2ffe0bac248191988612d9081dd6b1-lark-hao/project`。旧热种子 `ou_28d4...` 仅作为过渡兼容。
- WebDock 拉取结果：`/var/lib/webdock/browser_data/feishu_projects.json` 已包含同一条 Feishu 路由。
- `/新对话` 合成验证：`om_codex_newchat_003` 返回新会话确认；随后 `om_codex_newchat_004` 进入新 conversation `.../c/6a338e4e-1e60-83e8-8307-83f2f8c997f6` 并返回测试 token。
- OpenClaw bridge：`/v1/models` OK，OpenClaw gateway `/healthz` OK。

## 剩余阻塞

- 生产镜像仍需发布包含 Bitable bootstrap 的新版本；发布后可用一次性 `FEISHU_<PROFILE>_SESSION_CONSOLE_BOOTSTRAP=true` 初始化管理台，后续走数据库登记的 `external_sources`。表级 `APP_TOKEN/TABLE_ID` 不再要求常驻；自建应用 `APP_ID/APP_SECRET` 和 Bitable 权限仍需要安全来源。
- 当前 OpenClaw bridge 收到的 Feishu 元数据只有 `channel/chat_type/peer_id/message_id`，未看到 `mentions` 或原始群事件。群消息「全量记录但仅 @ 机器人回复」不能靠文本猜测上线，应接入原始 Feishu webhook，或让 OpenClaw 明确转发 `mentions` 后再启用门控。

## 后续建议

1. 正式发布 AliECS doc-sync-worker 镜像后，用 bootstrap 开关运行一次 `sync-feishu-full` 创建并登记管理台。
2. bootstrap 成功后移除一次性开关，保留数据库 source 记录。
3. 接入 Feishu 原始消息 webhook，落库消息日志/回复任务，再启用群 @ 门控。
