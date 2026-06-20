# Feishu Group Policy, Dynamic Timeout, and Bitable Controls Design

## Scope

This change completes four related runtime fixes without a formal deployment or Git push:

1. Remove OpenClaw's Feishu mention helper text and the bot's own leading mention before forwarding a group prompt to ChatGPT.
2. Make group reply behavior default to `回复所有`, while allowing `群表.回复模式=仅@回复` and `是否启用机器人=false` to override it.
3. Replace WebDock's absolute 120-second response deadline with a 120-second soft deadline, a 15-second no-progress timeout, and a 20-minute hard deadline.
4. Convert stable control/status text fields in the Feishu Base to single-select fields in place, preserving relation fields and existing values.

## Bridge behavior

- Feishu-only cleaning removes the two exact trailing `[System: ... mention ...]` helper lines.
- When those helper lines identify a bot mention, remove only the leading bot mention token; preserve the rest of the user's text and all attachments.
- New groups are created with `回复模式=回复所有`.
- Existing group records retain manually controlled fields (`是否启用机器人`, `是否记录全量消息`, `回复模式`, `风险级别`) during normal upserts.
- Group policy is read from `群表` with a 15-second cache. Missing/blank/invalid modes fall back to `回复所有`; lookup failures also fall back to `回复所有` and emit a diagnostic log.
- `仅@回复` requires a real bot mention; disabled groups never call WebDock but are still recorded.

## WebDock waiting behavior

- `CHAT_TIMEOUT_SECONDS=120` remains the soft deadline.
- A progress fingerprint covers assistant text, assistant count, streaming/stop state, image-generation state, generated-image URLs, and widget appearance.
- After the soft deadline, every fingerprint change renews a 15-second observation window.
- No change for 15 seconds after the soft deadline returns `RESPONSE_TIMEOUT`.
- `RESPONSE_HARD_TIMEOUT_SECONDS=1200` always stops the wait at 20 minutes.
- The bridge HTTP timeout is raised to 1260 seconds so it cannot terminate before WebDock.

## Bitable fields

The migration updates existing field IDs in place to type 3 (`SingleSelect`) after validating all non-empty record values against the configured options. It never deletes fields. Existing relation fields remain type 18 (`SingleLink`) and are verified after migration.
The historical reply-mode alias `全部回复` is normalized to the canonical `回复所有` before conversion; unknown values stop the migration.

Single-select targets:

- 会话索引表：会话类型、会话状态
- 消息日志表：聊天类型、命令类型（`无`、`/新对话`、`/重置`、`/摘要`）、处理状态
- 回复任务表：任务类型、任务状态、审核状态
- 群表：群类型、回复模式、风险级别
- 用户表：用户状态、用户角色
- 规则配置表：规则对象类型、回复模式

Checkbox, ID, URL, free-text, dynamic message-type, and relation fields are unchanged.

## Verification and rollout

- Use red/green tests for every behavior change.
- Run the complete Bridge and WebDock test suites.
- Hot-copy changed Python files into the running containers, update only timeout runtime variables, restart the affected processes/containers, run the field migration, and verify hashes, health, Base schema/options/relations, and live permission state.
- Do not commit, push, build release images, or formally deploy until the user accepts the hot-update result.
