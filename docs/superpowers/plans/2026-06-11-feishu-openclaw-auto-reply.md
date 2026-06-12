# Feishu OpenClaw Auto Reply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect Feishu to the ECS-hosted OpenClaw gateway so private chats and group chats can auto-reply through the existing OpenClaw -> openclaw-bridge -> WebDock -> ChatGPT path.

**Architecture:** Prefer OpenClaw's native Feishu/Lark channel in WebSocket mode on `服务器:/root/openclaw`. Do not expose an OpenClaw Feishu webhook unless WebSocket is impossible. AliECS remains the public app/API stack and optional observability layer; `feishu-obsidian-miner` is a reference for Feishu auth/bitable patterns only, not a callback implementation.

**Tech Stack:** OpenClaw gateway, Feishu self-built app bot, Feishu `im.message.receive_v1`, Feishu message send/reply APIs, existing `openclaw-bridge` at `127.0.0.1:18080`, existing AliECS docs/deploy scripts.

---

## Sources To Recheck Before Execution

- OpenClaw Feishu channel docs: https://docs.openclaw.ai/channels/feishu
- OpenClaw security advisory for Feishu webhook mode: https://github.com/openclaw/openclaw/security/advisories/GHSA-xh72-v6v9-mwhc
- Feishu receive message event: https://open.feishu.cn/document/server-docs/im-v1/message/events/receive
- Feishu send message API: https://open.feishu.cn/document/server-docs/im-v1/message/create
- Feishu reply message API: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/reply
- Feishu custom app tenant token: https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal

## Fixed Decisions

- Use OpenClaw native Feishu channel first. It already supports bot DMs and group chats, and its docs require OpenClaw `2026.5.29` or later.
- Use WebSocket mode first. This avoids a public Feishu webhook surface on OpenClaw and avoids the older fail-open webhook class fixed in OpenClaw `2026.4.15`.
- For the user's target of all private and group chats:
  - Final desired config: DMs open, group policy open, mention requirement disabled.
  - Safe rollout config: start with one allowlisted DM and one allowlisted group, then widen to all groups after one live proof.
- Do not reuse `services/backend-api/app/routers/webhooks/feishu.py` for the main path unless OpenClaw native Feishu cannot satisfy the requirement.
- Do not copy `feishu-obsidian-miner` into AliECS. Reuse only its Feishu auth/client/source parsing ideas if fallback code is needed.

## File And Runtime Map

Main path, mostly runtime configuration:

- Runtime only: `服务器:/root/openclaw` OpenClaw account/channel config.
- Runtime only: Feishu developer console app settings, permissions, event subscriptions, app publishing.
- Existing runtime: `服务器:/opt/openclaw-bridge/openclaw_bridge.py`.
- Existing source docs to update after proof:
  - `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS\docs\ops\three-host-architecture.md`
  - `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS\docs\ops\ai-handoff-rules.md`
  - `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS\docs\webdock-openclaw-integration.md`

Fallback adapter path, only if native OpenClaw channel is blocked:

- Modify: `services/backend-api/app/routers/webhooks/feishu.py`
- Create: `services/backend-api/app/integrations/feishu/crypto.py`
- Create: `services/backend-api/app/integrations/feishu/schemas.py`
- Create: `services/backend-api/app/integrations/feishu/handlers.py`
- Create: `services/feishu-bot-worker/app/main.py`
- Create: `services/feishu-bot-worker/app/openclaw_client.py`
- Create: `services/feishu-bot-worker/app/feishu_client.py`
- Create: `db/migrations/00xx_feishu_bot_jobs.sql`
- Modify: `deploy/ecs/compose.prod.yml`
- Modify: `local/docker-compose.local.yml`
- Modify: `deploy/ecs/runtime.env.example`
- Test: `tests/test_backend_feishu_webhook.py`
- Test: `tests/test_feishu_bot_worker.py`

## Task 1: Baseline ECS And OpenClaw State

**Files:** none.

- [ ] Check OpenClaw version and channel support.

Run:

```powershell
ssh aliecs 'cd /root/openclaw && openclaw --version && openclaw channels --help | sed -n "1,120p"'
```

Expected:

- Version is `2026.5.29` or newer.
- `channels login --channel feishu` is available.

- [ ] If OpenClaw is older, upgrade before adding Feishu.

Run:

```powershell
ssh aliecs 'cd /root/openclaw && openclaw update && openclaw --version'
```

Expected:

- Version remains at least `2026.5.29`.
- Never proceed with Feishu webhook mode on OpenClaw older than `2026.4.15`.

- [ ] Confirm current reply path still works before adding a new channel.

Run:

```powershell
ssh aliecs 'curl -fsS http://127.0.0.1:18080/v1/models'
ssh webdock 'curl -fsS http://127.0.0.1:18000/healthz'
```

Expected:

- Bridge returns an OpenAI-compatible model list.
- WebDock health is OK.

## Task 2: Prepare Feishu Self-Built App

**Files:** none.

- [ ] Decide app boundary.

Recommended:

- Create a separate Feishu app named `Hydwang OpenClaw Bot`.
- Do not reuse the bitable sync app unless permissions, audit, and secret rotation are acceptable.

- [ ] Enable bot capability in the Feishu developer console.

Required permissions:

- Receive/read messages: `im:message`
- Send as bot: `im:message:send_as_bot`
- Read resources from messages, if images/files may be supported: `im:resource`
- Read chat metadata: `im:chat`, `im:chat:readonly`
- Optional sender resolution: `contact:user.id:readonly`

- [ ] Configure events.

Use event `im.message.receive_v1`. Prefer OpenClaw WebSocket/long-connection setup through the OpenClaw wizard. If the console still requires event selection, subscribe to receive-message events for bot DMs and group chats.

- [ ] Publish a new app version and wait for admin approval if required.

Expected:

- App has App ID and App Secret.
- Bot can be added to at least one test group.
- One test private chat and one test group are available for smoke testing.

## Task 3: Login Feishu Channel In OpenClaw

**Files:** runtime config under `服务器:/root/openclaw`; do not commit secrets.

- [ ] Snapshot current OpenClaw config before changing it.

Run:

```powershell
ssh aliecs 'cd /root/openclaw && mkdir -p backups && tar -czf backups/openclaw-before-feishu-$(date +%Y%m%d-%H%M%S).tgz config* accounts* 2>/dev/null || true'
```

Expected:

- A timestamped backup exists, or command reports missing optional files only.

- [ ] Run OpenClaw Feishu login.

Run:

```powershell
ssh aliecs 'cd /root/openclaw && openclaw channels login --channel feishu'
```

Input:

- Prefer manual setup if QR does not work with domestic Feishu.
- Paste App ID and App Secret only in the SSH session or OpenClaw prompt, never in Git or chat.

- [ ] Restart OpenClaw gateway.

Run:

```powershell
ssh aliecs 'cd /root/openclaw && openclaw gateway restart && openclaw logs --tail 120'
```

Expected:

- Feishu account starts without auth or permission errors.
- Gateway still has existing Weixin channels.

## Task 4: Safe Initial Access Policy

**Files:** runtime config under `服务器:/root/openclaw`.

- [ ] Start with narrow access for first proof.

Config intent:

```json5
{
  channels: {
    feishu: {
      dmPolicy: "allowlist",
      allowFrom: ["ou_test_user_open_id"],
      groupPolicy: "allowlist",
      groupAllowFrom: ["oc_test_group_chat_id"],
      requireMention: true,
      streaming: false,
      typingIndicator: true
    }
  }
}
```

Expected:

- Test user DM is allowed.
- Test group replies only when the bot is mentioned.
- Other groups/users are ignored during smoke tests.

- [ ] After proof, switch to the requested broad mode.

Final config intent:

```json5
{
  channels: {
    feishu: {
      dmPolicy: "open",
      allowFrom: ["*"],
      groupPolicy: "open",
      requireMention: false,
      streaming: false,
      typingIndicator: true
    }
  }
}
```

Expected:

- Private chats auto-reply.
- All groups where the bot is present auto-reply without @mention.
- If noise is too high, revert to `groupPolicy: "allowlist"` or `requireMention: true`.

## Task 5: Live Verification Ladder

**Files:** none.

- [ ] Verify gateway logs while sending one private message.

Run:

```powershell
ssh aliecs 'cd /root/openclaw && openclaw logs --follow'
```

Manual test:

- Send `状态测试：请回复 feishu-dm-ok` to the bot in private chat.

Expected:

- Feishu event is received.
- OpenClaw routes to the existing model/provider.
- Reply appears in Feishu.

- [ ] Verify group mention mode.

Manual test:

- Add bot to test group.
- Send `@Bot 状态测试：请回复 feishu-group-mention-ok`.

Expected:

- Reply appears in group.
- No duplicate replies.

- [ ] Verify group no-mention mode after broad config.

Manual test:

- Send `状态测试：请回复 feishu-group-open-ok` without mentioning the bot.

Expected:

- Reply appears in group.
- Logs show group chat route accepted.

- [ ] Confirm the bridge and WebDock path was actually used.

Run:

```powershell
ssh aliecs 'journalctl -u openclaw-bridge.service -n 120 --no-pager'
ssh webdock 'journalctl -u webdock -n 120 --no-pager'
```

Expected:

- The request passed through `openclaw-bridge`.
- WebDock saw one real `/chat/completions` style request.

## Task 6: Production Hardening

**Files:** runtime config first; source docs after proof.

- [ ] Set a fallback behavior for OpenClaw/ChatGPT timeout.

Expected behavior:

- Timeout should send one short Feishu reply such as `暂时没有生成回复，请稍后再试。`
- Timeout should not retry endlessly in group chats.

- [ ] Confirm dedupe behavior.

Manual test:

- Resend same Feishu message only by platform retry if possible, or inspect logs for duplicate event IDs.

Expected:

- One inbound message produces one reply.
- Duplicates are ignored or coalesced.

- [ ] Confirm group blast risk.

Manual test:

- In a busy group, enable broad mode only after one low-traffic window.

Expected:

- Auto-reply volume is acceptable.
- If not acceptable, keep `groupPolicy: "allowlist"` and add groups gradually.

## Task 7: Documentation Update After Proof

**Files:**

- Modify: `docs/ops/three-host-architecture.md`
- Modify: `docs/ops/ai-handoff-rules.md`
- Modify: `docs/webdock-openclaw-integration.md`

- [ ] Add Feishu runtime chain.

Add this path:

```text
Feishu private/group chats
-> 服务器 OpenClaw native Feishu channel
-> 服务器 OpenClaw gateway
-> 服务器 existing model/provider binding
-> 服务器 openclaw-bridge at 127.0.0.1:18080
-> 服务器 reverse tunnel endpoint at 127.0.0.1:11800
-> 旧电脑 WebDock
-> 旧电脑 Chrome / ChatGPT web session
-> reply back through the same path
```

- [ ] Document rollback.

Minimum rollback commands:

```powershell
ssh aliecs 'cd /root/openclaw && openclaw channels logout --channel feishu || true && openclaw gateway restart'
```

- [ ] Run docs-only diff check.

Run:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS diff -- docs/ops/three-host-architecture.md docs/ops/ai-handoff-rules.md docs/webdock-openclaw-integration.md
```

Expected:

- Docs describe observed behavior, not assumed behavior.

## Task 8: Fallback Adapter Only If Native Channel Fails

**Use this task only if one of these is true:**

- OpenClaw native Feishu channel is unavailable on the ECS version after safe upgrade.
- Feishu tenant policy blocks OpenClaw WebSocket/long-connection mode.
- OpenClaw cannot meet the required routing or audit behavior.

**Files:** see fallback file map above.

- [ ] Implement FastAPI challenge, token check, and encrypted payload support in `services/backend-api/app/integrations/feishu/`.

Test command:

```powershell
python -m unittest discover -s tests -v
```

Expected:

- URL verification challenge returns the exact challenge response.
- Missing or mismatched `FEISHU_VERIFICATION_TOKEN` fails closed.
- Missing `FEISHU_ENCRYPT_KEY` fails closed when encrypted mode is enabled.

- [ ] Store accepted events in `integration_events` and create bot jobs in a new durable queue table.

Expected:

- Webhook returns quickly.
- Long OpenClaw calls happen only in worker.
- Unique `(provider,event_id)` prevents duplicate replies.

- [ ] Build `feishu-bot-worker` to call OpenClaw and reply to Feishu.

Expected:

- Worker uses tenant token from app ID/secret.
- Worker calls `http://host.docker.internal:18080/v1/chat/completions` or another verified ECS-local OpenClaw endpoint.
- Worker uses Feishu reply API for message-thread replies and send API when no source `message_id` is available.

- [ ] Add Compose and env placeholders.

Expected:

- Real App Secret, verification token, encrypt key, and tenant token never enter Git.
- `runtime.env.example` contains placeholders only.

- [ ] Verify fallback adapter locally with fake events, then on ECS with Feishu console challenge and one real DM/group.

Expected:

- Same live proof as Tasks 5 and 6.

## Final Acceptance Criteria

- OpenClaw version is `2026.5.29` or newer.
- Feishu app is published with bot capability and receive-message/send-message permissions.
- Private chat produces one correct reply.
- Group chat produces one correct reply in mention mode, then in no-mention mode if broad config is enabled.
- Existing Weixin A/B/C reply path still works after Feishu is enabled.
- `openclaw-bridge` and WebDock logs prove traffic still goes through the existing ChatGPT browser path.
- No real Feishu secrets or tokens are committed, logged, or pasted into chat.
- Rollback command is documented and tested enough to disable Feishu quickly.
