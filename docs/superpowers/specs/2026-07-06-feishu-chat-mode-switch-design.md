# 飞书自主切换 ChatGPT 对话模式（极速/均衡/高级）设计

日期：2026-07-06
状态：已确认（用户批准）
涉及仓库：AliECS（bridge：`deploy/openclaw-bridge/openclaw_bridge.py`）、webdock（`src/browser/`、`src/api/`）

## 背景与目标

ChatGPT 网页在输入区提供对话模式选择器（当前中文界面显示：极速/均衡/高级）。WebDock 的
`ChatGPTPage.ask` 从不操作该选择器，所以所有飞书会话永远使用账号上次停留的模式（现为
"高级"）。目标：飞书用户在聊天里发命令即可按会话切换模式。

已确认的产品决策：

- 控制入口：聊天命令 `/模式 极速|均衡|高级`（与 `/新对话` 同一命令通道）。
- 生效范围：按会话（peer）粘性，直到再次更改；`/新对话` 后仍保留。
- 架构：方案 A —— bridge 持有状态并随每次请求下发 metadata，WebDock 发送前校准选择器。

## 1. 命令层（bridge）

- `feishu_command_type` 命令表增加 `/模式`。
- 命中 `/模式` 时 bridge 短路处理：更新状态后直接把确认文案作为本次 chat completion 的
  回复返回（如"已切换为极速模式"），不把该消息转发给 WebDock/ChatGPT。
- `/模式`（无参数）或参数非法：回复当前模式 + 用法提示，不改状态。
- 群聊触发沿用现有规则（@机器人 等），不新增触发路径。
- 该命令仅飞书通道生效；微信链路不解析、行为不变。

## 2. 状态存储（按 peer 粘性）

- 内存：`peer → mode` 映射，带锁 + 短缓存，复用 `feishu_group_reply_policy` 的
  缓存模式（`_feishu_group_policy_cache` 同款结构与 TTL）。
- 持久化：飞书会话台 bitable 新增单选列"对话模式"（选项：极速/均衡/高级/默认）：
  - 私聊写**用户表**（`FEISHU_SESSION_CONSOLE_USER_TABLE_ID`）；
  - 群聊写**群表**（`FEISHU_SESSION_CONSOLE_GROUP_TABLE_ID`）。
  - 不用会话表：会话表记录随 `/新对话` 换代，存那里会丢粘性。
- bridge 重启后从表恢复；bitable 不可用时退化为纯内存（只打日志，不阻断命令与请求）。
- "默认"/无记录 = 不下发 `chatgpt_mode`，WebDock 不碰选择器。
- ⚠️ 建列注意先建列再写值（07-04 规则表上线时踩过 Bitable 建列先于写值的坑）。

## 3. 传递（bridge → WebDock）

- bridge 组装请求时，若该 peer 有已设置的模式，则在 metadata 增加
  `chatgpt_mode ∈ {"fast","balanced","advanced"}`（规范值，与 UI 文案解耦）；
  未设置则完全不带此键。
- 中文命令词到规范值映射：极速→fast、均衡→balanced、高级→advanced。
- webdock `LaneContext.from_metadata` 增加 `chatgpt_mode` 字段（无此键 = None）。

## 4. 执行（WebDock）

- `ChatGPTPage.ask` 在粘贴消息之前调用 `ensure_mode(target)`：
  1. 读模式选择器当前标签；与目标一致 → 直接返回（零额外开销）。
  2. 不一致 → 点开下拉，按文本匹配目标项（中英文标签都锚），点击后复核标签。
- **每次发送前校准**而非切换时点一次：多个飞书会话串行共享同一浏览器 worker，
  必须防止 A 会话切换后污染 B 会话。
- `selectors.py` 新增选择器组（模式选择器按钮、下拉菜单、菜单项）。
  ⚠️ 实际 DOM 未知：实施第一步必须在真机 CDP 上探明选择器与中英文实际文案
  （极速/Instant、均衡/Auto 或 Balanced、高级/Thinking 均为待验证假设），再落代码。
- `lane.chatgpt_mode is None` 时不调用 `ensure_mode`，行为与现状完全一致
  （存量微信链路零影响）。

## 5. 错误处理

- `ensure_mode` 任一步失败（选择器找不到 / 点击失败 / 复核不符）：不抛错、不阻断，
  照常发送消息，打 `mode_switch_failed` 结构化日志。原则：宁可用错模式出答案，
  不要不出答案。
- bitable 读写失败：命令仍即时生效（内存态），持久化丢失只打日志。
- bridge 下发了 `chatgpt_mode` 但 WebDock 是旧版本（不认识该字段）：字段被忽略，
  行为退回现状，天然向后兼容；反向（新 WebDock + 旧 bridge）同理。

## 6. 测试与验证

- bridge 单测：命令解析（含无参/非法参）、短路回复、状态读写、bitable 降级路径。
- webdock 单测（fake page）：模式一致跳过点击；不一致触发点击序列；失败不阻断 ask；
  `chatgpt_mode=None` 不触碰选择器。
- 真机端到端：飞书发 `/模式 极速` → 收到确认 → 提问并在网页端确认选择器停在"极速"
  且回复正常 → `/模式 高级` 切回复测 → 重建 bridge 容器后确认模式从 bitable 恢复。

## 非目标

- 微信通道的模式切换。
- 按单条消息临时指定模式。
- 在尾泡/诊断行展示当前模式（可作后续增强，不在本期）。
