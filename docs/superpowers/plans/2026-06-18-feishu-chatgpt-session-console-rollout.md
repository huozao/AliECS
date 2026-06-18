# Feishu ChatGPT Session Console Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy the Feishu ChatGPT session console so `/新对话` reliably opens a fresh ChatGPT project conversation, Feishu sessions are controlled by the Bitable session index, group messages are logged with @-mention gating, and rich ChatGPT web/widget replies are delivered to Feishu as usable text or screenshots.

**Architecture:** Keep the current three-host chain: Feishu -> `服务器` OpenClaw/bridge -> `旧电脑` WebDock -> ChatGPT web -> Feishu. Use the Feishu Bitable as the human-facing control plane, Postgres as the runtime cache/source for route APIs, and WebDock `feishu_projects.json` + `lane_state.json` as the browser routing state. Implement in slices: routing first, reply rendering second, logging/tasks third, group mention gating last after raw event evidence.

**Tech Stack:** Python, FastAPI, Postgres, Feishu Bitable APIs, OpenClaw bridge, WebDock Playwright browser automation, pytest/unittest, GitHub Actions deploy, SSH verification on `服务器` and `旧电脑`.

---

## Execution Rules

- Work from `开发机` only. AliECS repo: `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS`; WebDock repo: `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\webdock`.
- Use `ssh aliecs` for `服务器`, `ssh webdock` for `旧电脑`.
- Keep AliECS and WebDock commits separate. Do not run Git in the top-level `AliECS-WebDock` directory.
- Use RED -> GREEN for every code slice: add a failing targeted test, implement the smallest fix, run the targeted test, then run the relevant local gate.
- Do a hot update for online checking first. After production behavior is confirmed, do the formal GitHub deploy/release flow.
- Never commit secrets, Bitable data dumps, browser profiles, `.env`, `logs`, `browser_data`, or `_references`.
- Hard stops only: missing Feishu app credentials/table IDs, expired ChatGPT login on `旧电脑`, Feishu platform not delivering the raw fields required for mention gating, or SSH unavailable.

## Known Current State

- Existing design doc: `AliECS/docs/feishu-session-console-bitable-design.md`.
- Backend route source: `services/backend-api/app/main.py::_routing_projects()` reads `managed_contacts` and exposes `/v1/routing/feishu-projects.json`.
- Existing runtime route table: `db/migrations/0014_managed_contacts.sql`.
- Existing Feishu Bitable puller: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`, but it currently only stores external records and does not sync Feishu rows into `managed_contacts`.
- WebDock already has channel-aware Feishu routing: `webdock/src/browser/lane_routing.py`, `webdock/src/browser/routing_pull.py`, `webdock/src/browser/lane_scheduler.py`.
- Current `/新对话` root cause: backend Feishu route source is empty, so WebDock writes `{"lanes":{}}` into `feishu_projects.json`; `LaneRouter.resolve_target_url(..., force_new=True, channel="feishu")` returns `None`, so the page never navigates to the project home.
- Current rich reply problem: WebDock screenshots rich widgets by cloning DOM into a white standalone page. A dark ChatGPT widget can keep white text but lose the dark inherited background, producing a Feishu image with visible bullets and missing text. Feishu supports rich text/cards/images, but not arbitrary ChatGPT webpage DOM as a native reply; use Markdown text when available, PNG screenshot when not.

Official Feishu references to re-check during execution:

- Receive message event: `https://open.feishu.cn/document/server-docs/im-v1/message/events/receive`
- Send message content structure: `https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/im-v1/message/create_json`
- Bot/message card guidance: `https://open.feishu.cn/document/ukzMukzMukzM/uMjNyYjLzYjM24yM2IjN`

---

## File Map

AliECS:

- Modify: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`
- Modify: `services/doc-sync-worker/app/pipelines/managed_contacts.py`
- Modify: `tests/test_managed_contacts_sync.py`
- Modify: `tests/test_doc_sync_worker.py`
- Modify: `services/backend-api/app/main.py`
- Modify: `tests/test_routing_api.py`
- Modify later: `deploy/openclaw-bridge/openclaw_bridge.py`
- Modify later: `tests/test_openclaw_bridge.py`
- Optional create: `db/migrations/0015_feishu_session_console.sql`
- Optional create: `services/backend-api/app/routers/feishu_console.py`

WebDock:

- Modify: `src/browser/chatgpt_page.py`
- Modify: `tests/test_widget_render.py`
- Modify: `tests/test_rich_markdown.py`
- Modify if needed: `src/browser/lane_routing.py`
- Modify if needed: `tests/test_feishu_lane_routing.py`

Runtime paths:

- `服务器:/root/AliECS`
- `服务器:/root/openclaw`
- `服务器:/opt/openclaw-bridge`
- `旧电脑:/opt/webdock`
- `旧电脑:/var/lib/webdock/browser_data/feishu_projects.json`
- `旧电脑:/var/lib/webdock/browser_data/lane_state.json`

---

## Phase 0: Baseline Evidence

- [ ] **Step 0.1: Capture local Git state**

Run serially:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS status --short --branch
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\webdock status --short --branch
```

Expected: note untracked docs separately. Do not delete or revert them.

- [ ] **Step 0.2: Confirm route source is empty or incomplete**

```bash
ssh aliecs 'curl -fsS http://127.0.0.1:8000/v1/routing/feishu-projects.json || curl -fsS http://127.0.0.1:8082/v1/routing/feishu-projects.json'
ssh webdock 'python3 -m json.tool /var/lib/webdock/browser_data/feishu_projects.json 2>/dev/null || true'
ssh webdock 'python3 -m json.tool /var/lib/webdock/browser_data/lane_state.json 2>/dev/null | grep -E "feishu|ou_|/c/" | tail -80 || true'
```

Expected failure signature: route JSON has empty `lanes` or no `ou_28d4...` lane.

- [ ] **Step 0.3: Confirm current runtime health**

```bash
ssh aliecs 'cd /root/openclaw && docker compose run --rm -T openclaw-cli channels status --deep'
ssh aliecs 'curl -fsS http://127.0.0.1:18080/v1/models'
ssh aliecs 'curl -fsS http://127.0.0.1:11800/healthz'
ssh webdock 'curl -fsS http://100.97.176.57:18000/healthz'
```

Expected: Feishu channel enabled/running, bridge models OK, WebDock health OK.

---

## Phase 1: Make Bitable Session Index Drive Feishu Routing

Purpose: get `/新对话` working by feeding `/v1/routing/feishu-projects.json` from the Bitable `会话索引表` through `managed_contacts`.

- [ ] **Task 1.1: RED test for session index row -> managed contact**

Modify `tests/test_managed_contacts_sync.py`.

Add a test proving a row from `会话索引表` becomes a Feishu `managed_contacts` route only when it is current and active/waiting:

```python
def test_feishu_session_index_upserts_current_active_session_route(self) -> None:
    from app.pipelines.managed_contacts import sync_managed_contacts_from_sheet

    store = FakeContactStore()
    changed = sync_managed_contacts_from_sheet(
        store,
        "会话索引表",
        [
            {
                "session_key": "tenant-a:user:ou_28d4",
                "会话类型": "私聊",
                "飞书用户名": "hao",
                "ChatGPT 项目名": "飞书 AI 会话台",
                "ChatGPT 对话链接": "https://chatgpt.com/g/g-p-lark/project",
                "会话状态": "活跃",
                "是否当前会话": True,
            }
        ],
    )

    self.assertEqual(1, changed)
    row = store.contacts[("feishu", "ou_28d4")]
    self.assertEqual("hao", row["display_name"])
    self.assertEqual("https://chatgpt.com/g/g-p-lark/project", row["project_url"])
    self.assertEqual("飞书 AI 会话台", row["project_name"])
```

Run:

```powershell
$env:PYTHONPATH='services/doc-sync-worker'; python -m unittest tests.test_managed_contacts_sync.ManagedContactsSyncTests.test_feishu_session_index_upserts_current_active_session_route -v
```

Expected: FAIL because `会话索引表` is ignored.

- [ ] **Task 1.2: Implement Bitable session row normalization**

Modify `services/doc-sync-worker/app/pipelines/managed_contacts.py`.

Rules:

- Accept sheet names `会话索引表`, `sessions`, `飞书会话索引表`.
- Read `session_key` aliases: `session_key`, `会话key`, `会话键`.
- Extract peer:
  - `tenant:user:open_id` -> `open_id`
  - `tenant:group:chat_id` -> `chat_id`
  - `tenant:group_user:chat_id:open_id` -> use `chat_id:open_id` only for internal logs; for WebDock routing v1 use `chat_id` unless a separate per-user project URL is present.
- Only sync rows where `会话状态` is `活跃` or `待创建`, and `是否当前会话` is true/yes/1/✓.
- Use project home URL ending `/project` for `project_url`. If the Bitable row stores a conversation `/c/...` URL, derive project home from the existing `ChatGPT 项目首页链接` field; if not present, keep the original URL but mark `notes` with `needs_project_home_url`.
- Map display name from `飞书用户名` or `飞书群名`.

Run:

```powershell
$env:PYTHONPATH='services/doc-sync-worker'; python -m unittest tests.test_managed_contacts_sync -v
```

Expected: PASS.

- [ ] **Task 1.3: RED test for Feishu full sync writing managed contacts**

Modify `tests/test_doc_sync_worker.py`.

Add or extend a Feishu sync test so `_sync_bitable_records()` calls `sync_managed_contact_from_row()` with the Feishu source/table name and increments `managed_contact_count`.

Run:

```powershell
$env:PYTHONPATH='services/doc-sync-worker'; python -m unittest tests.test_doc_sync_worker -v
```

Expected before implementation: FAIL because Feishu sync does not update `managed_contacts`.

- [ ] **Task 1.4: Implement managed contact sync in Feishu puller**

Modify `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`.

In `_sync_bitable_records()`, after `snapshot = build_record_snapshot(...)` and before/after `store.upsert_record(...)`, call `sync_managed_contact_from_row(store, source_name, snapshot.normalized_json)` exactly like WeCom does.

Run:

```powershell
$env:PYTHONPATH='services/doc-sync-worker'; python -m unittest tests.test_doc_sync_worker tests.test_managed_contacts_sync -v
```

Expected: PASS.

- [ ] **Task 1.5: Backend routing API regression**

Modify `tests/test_routing_api.py` only if needed.

Ensure `/v1/routing/feishu-projects.json` returns only enabled `channel='feishu'` contacts with non-empty project URL and keeps WeChat isolated.

Run:

```powershell
$env:PYTHONPATH='.'; python -m unittest tests.test_routing_api -v
```

Expected: PASS.

---

## Phase 2: Fix Rich Web/Widget Reply Delivery to Feishu

Purpose: solve the image issue shown by the two screenshots. Do not try to send arbitrary ChatGPT webpage DOM as a Feishu native rich message. Deliver Markdown when the reply has `.markdown`; deliver PNG media for widgets/generated images.

- [ ] **Task 2.1: RED test that widget screenshot prefers live page rendering**

Modify `webdock/tests/test_widget_render.py`.

Add an async test or minimal fake-object test proving `_screenshot_widget()` calls `widget.screenshot()` first and only uses clone rendering if live screenshot fails.

Expected behavior:

- Direct live screenshot returns bytes -> use those bytes.
- Live screenshot raises -> fallback to `_INLINE_STYLES_JS` clone path.

Run:

```powershell
pytest tests/test_widget_render.py -v
```

Expected before implementation: FAIL because current code always clones into a standalone white page.

- [ ] **Task 2.2: Implement live screenshot first**

Modify `webdock/src/browser/chatgpt_page.py`.

Implementation rule:

1. Keep `await widget.scroll_into_view_if_needed(...)`.
2. Try `return await widget.screenshot(timeout=8000)` first.
3. If that fails or returns tiny/empty bytes, fall back to the existing clone renderer.
4. Keep `_build_render_html()` tests green, because clone fallback remains useful.

Run:

```powershell
pytest tests/test_widget_render.py tests/test_detector_response_wait.py tests/test_chatgpt_image_reply.py -v
```

Expected: PASS.

- [ ] **Task 2.3: Verify Feishu Markdown is not overused for widget-only replies**

Run:

```powershell
pytest tests/test_rich_markdown.py tests/test_openai_chat_completions.py -v
```

Expected: PASS. For a widget-only reply, `answer` may be empty text and the screenshot media token must become the content.

- [ ] **Task 2.4: Runtime visual verification on `旧电脑`**

After hot update, send a synthetic WebDock request with Feishu metadata and a prompt that produces a structured visual/list reply. Verify the archive contains a `MEDIA:` URL and the attached PNG is visually correct:

```bash
ssh webdock 'curl -fsS http://100.97.176.57:18000/healthz'
ssh webdock 'find /var/lib/webdock/browser_data/archive -type f -name "*.json" -mmin -30 | tail -5'
```

If the returned image still loses text, inspect the latest WebDock debug screenshot and switch `_screenshot_widget()` to live screenshot only for Feishu widgets, leaving clone fallback for non-Feishu channels.

---

## Phase 3: `/新对话` End-to-End Closure

- [ ] **Task 3.1: Seed one Bitable session index row**

In Feishu Bitable `会话索引表`, create or confirm one current row:

```text
session_key = <tenant_key>:user:ou_28d4f058cbd2a13f3fcc6fd575023e8e
会话类型 = 私聊
飞书用户名 = hao
ChatGPT 项目名 = 飞书 AI 会话台
ChatGPT 项目首页链接 = https://chatgpt.com/g/g-p-6a2ffe0bac248191988612d9081dd6b1-lark-hao/project
ChatGPT 对话链接 = https://chatgpt.com/g/g-p-6a2ffe0bac248191988612d9081dd6b1-lark-hao/project
会话状态 = 待创建
是否当前会话 = TRUE
会话版本 = current + 1
```

Do not store secrets in the table.

- [ ] **Task 3.2: Run Feishu Bitable sync on `服务器`**

Use the configured profile. If the profile is unknown, read `FEISHU_ENV_PROFILES` presence only; do not print secrets.

```bash
ssh aliecs 'cd /root/AliECS && docker compose --env-file deploy/ecs/runtime.env -f deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main sync-feishu-full'
```

Expected: sync success and `managed_contact_count >= 1`.

- [ ] **Task 3.3: Verify backend route JSON**

```bash
ssh aliecs 'curl -fsS http://127.0.0.1:8082/v1/routing/feishu-projects.json | python3 -m json.tool'
```

Expected:

```json
{
  "lanes": {
    "ou_28d4f058cbd2a13f3fcc6fd575023e8e": {
      "name": "hao",
      "project_url": "https://chatgpt.com/g/g-p-6a2ffe0bac248191988612d9081dd6b1-lark-hao/project"
    }
  }
}
```

- [ ] **Task 3.4: Verify WebDock pulled Feishu route**

Wait 70 seconds, then:

```bash
ssh webdock 'python3 -m json.tool /var/lib/webdock/browser_data/feishu_projects.json | grep -A4 ou_28d4'
```

Expected: same `project_url`.

- [ ] **Task 3.5: Synthetic `/新对话` proof**

Send an OpenClaw-equivalent POST to `服务器` bridge with Feishu metadata:

```bash
ssh aliecs 'curl -fsS http://127.0.0.1:18080/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"echo\",\"messages\":[{\"role\":\"user\",\"content\":\"Sender (untrusted metadata):\n```json\n{\\\"name\\\":\\\"hao\\\",\\\"id\\\":\\\"ou_28d4f058cbd2a13f3fcc6fd575023e8e\\\"}\n```\n\n[message_id: om_codex_newchat_001]\nhao: /新对话\"}],\"metadata\":{\"peer_id\":\"user:ou_28d4f058cbd2a13f3fcc6fd575023e8e\",\"chat_type\":\"private\",\"message_id\":\"om_codex_newchat_001\"},\"stream\":false}"'
```

Expected: bridge returns the new-conversation ack, WebDock clears the Feishu lane state, and no WeChat lane is touched.

- [ ] **Task 3.6: Synthetic post-new-chat message proof**

```bash
ssh aliecs 'curl -fsS http://127.0.0.1:18080/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"echo\",\"messages\":[{\"role\":\"user\",\"content\":\"Sender (untrusted metadata):\n```json\n{\\\"name\\\":\\\"hao\\\",\\\"id\\\":\\\"ou_28d4f058cbd2a13f3fcc6fd575023e8e\\\"}\n```\n\n[message_id: om_codex_newchat_002]\nhao: 请只回复 feishu-newchat-ok-20260618\"}],\"metadata\":{\"peer_id\":\"user:ou_28d4f058cbd2a13f3fcc6fd575023e8e\",\"chat_type\":\"private\",\"message_id\":\"om_codex_newchat_002\"},\"stream\":false}"'
ssh webdock 'python3 -m json.tool /var/lib/webdock/browser_data/lane_state.json | grep -A3 feishu:ou_28d4'
```

Expected: reply contains `feishu-newchat-ok-20260618`; lane state has a fresh `https://chatgpt.com/.../c/...` conversation URL under `feishu:ou_...`.

---

## Phase 4: Message Logs and Reply Tasks

Purpose: create the audit trail in Bitable. This can be implemented without blocking `/新对话`.

- [ ] **Task 4.1: Add runtime DB tables**

Create migration `db/migrations/0015_feishu_session_console.sql` with:

- `feishu_sessions`: `session_key`, `channel_peer_id`, `chat_type`, `user_open_id`, `chat_id`, `project_url`, `conversation_url`, `status`, `is_current`, `version`, timestamps, raw Bitable record id.
- `feishu_message_logs`: `event_id`, `message_id`, `tenant_key`, `chat_type`, `sender_open_id`, `chat_id`, `message_type`, `raw_content`, `clean_content`, `mentions_json`, `at_bot`, `need_chatgpt`, `skip_reason`, `session_key`, `status`, `reply_message_id`, `raw_json`, `error`.
- `feishu_reply_tasks`: `message_log_id`, `session_key`, `task_type`, `status`, `gpt_input`, `gpt_reply`, `needs_review`, `review_status`, timestamps, `send_result`, `failure_reason`.

Add indexes on `message_id`, `event_id`, `session_key`, `status`.

- [ ] **Task 4.2: Add tests for dedupe and state transitions**

Create `tests/test_feishu_session_console.py`.

Cover:

- duplicate `event_id` or `message_id` returns existing log;
- private message -> `need_chatgpt=True`;
- group no @ -> `need_chatgpt=False`, `skip_reason=未@机器人`, `status=仅记录`;
- `/新对话` -> session `待创建`, version increment, task type `新建会话`.

Run:

```powershell
$env:PYTHONPATH='services/backend-api'; pytest tests/test_feishu_session_console.py -v
```

- [ ] **Task 4.3: Implement backend service layer**

Create a small service module under `services/backend-api/app/services/feishu_session_console.py`.

Do not call Feishu/OpenClaw from this service. It only normalizes events, dedupes, writes DB rows, and computes routing/task decisions.

- [ ] **Task 4.4: Add Bitable write-back client**

Extend `services/doc-sync-worker/app/providers/feishu.py` or add a backend-local client if backend needs immediate writes.

Required methods:

- `create_record(app_token, table_id, fields)`
- `update_record(app_token, table_id, record_id, fields)`
- retry with redacted errors using the existing `_request_json()` pattern.

Write to Bitable asynchronously from a worker or best-effort backend queue; never let a Feishu user reply fail only because Bitable write-back is temporarily down. Persist write failures in DB and expose a retry command/view.

---

## Phase 5: Group @ Mention Gating

Purpose: implement the user's rule exactly: log all group messages, send to ChatGPT only when `mentions` proves the bot was mentioned, and strip the mention from the ChatGPT input.

- [ ] **Task 5.1: Capture real Feishu group event shape**

Ask a human to send two group messages:

1. normal group message without @bot;
2. group message with @bot and text after it.

Capture on `服务器`:

```bash
ssh aliecs 'docker logs --since=10m openclaw-openclaw-gateway-1 2>&1 | grep -iE "feishu|lark|mention|om_|oc_|ou_" | tail -200'
ssh aliecs 'docker logs --since=10m openclaw-bridge 2>&1 | grep -iE "bridge_request_trace|feishu|om_|oc_|ou_" | tail -200'
```

Decision:

- If OpenClaw forwards `mentions` or enough mention metadata to the bridge/backend, implement gating in the bridge/backend service.
- If OpenClaw does not forward `mentions`, do not guess from display text. Implement a direct Feishu message event receiver in `services/backend-api/app/routers/webhooks/feishu.py` and make that receiver the source of truth for group logging/gating. Keep OpenClaw Feishu group auto-reply disabled or scoped so duplicate replies cannot happen.

- [ ] **Task 5.2: RED tests for mention parsing**

Add tests covering the real captured shape.

Expected logic:

- `at_bot=True` only if `mentions` includes configured bot open_id/app id.
- `clean_content` removes the bot mention token/name but preserves the user's actual text.
- no @ in group -> create log only, no reply task.
- @ in group -> create log + reply task.

- [ ] **Task 5.3: Implement gating**

Implementation target depends on Task 5.1:

- OpenClaw metadata available: implement in `deploy/openclaw-bridge/openclaw_bridge.py` before forwarding to WebDock, and call backend logging endpoint.
- Raw event required: implement in `services/backend-api/app/routers/webhooks/feishu.py`, then forward only needed tasks to WebDock/bridge.

Do not enable group all-message auto-reply. Default group mode remains `仅@回复`.

---

## Phase 6: Deploy and Verify Loop

Run this loop until all gates pass. The executor should not stop after the first failed deploy; fix the owning layer, add/adjust a test, redeploy, and rerun the same gate.

### Local Gates

AliECS:

```powershell
$env:PYTHONPATH='.'; python -m unittest tests.test_routing_api tests.test_managed_contacts_sync tests.test_doc_sync_worker -v
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py -v
```

WebDock:

```powershell
pytest tests/test_widget_render.py tests/test_rich_markdown.py tests/test_feishu_lane_routing.py tests/test_lane_routing.py tests/test_chat_lane_scheduler.py tests/test_openai_chat_completions.py -v
```

If a command fails, record only the failing command and key error lines, then fix the smallest owning file.

### Hot Update

Use hot update for online check before formal release.

AliECS backend/doc-sync/bridge:

```bash
ssh aliecs 'cd /root/AliECS && git status -sb'
```

Apply only committed or clearly tracked source changes. Restart only affected services:

```bash
ssh aliecs 'cd /root/AliECS && docker compose --env-file deploy/ecs/runtime.env -f deploy/ecs/compose.prod.yml restart backend-api doc-sync-worker'
ssh aliecs 'sudo systemctl restart openclaw-bridge.service || docker restart openclaw-bridge'
```

WebDock:

```bash
ssh webdock 'cd /opt/webdock && git status -sb'
ssh webdock 'cd /opt/webdock && docker compose -f deploy/laptop/compose.yml restart webdock || sudo systemctl restart webdock'
```

### Runtime Verification

```bash
ssh aliecs 'cd /root/openclaw && docker compose run --rm -T openclaw-cli channels status --deep'
ssh aliecs 'curl -fsS http://127.0.0.1:18080/v1/models'
ssh aliecs 'curl -fsS http://127.0.0.1:11800/healthz'
ssh aliecs 'curl -fsS http://127.0.0.1:8082/v1/routing/feishu-projects.json | python3 -m json.tool'
ssh webdock 'curl -fsS http://100.97.176.57:18000/healthz'
ssh webdock 'python3 -m json.tool /var/lib/webdock/browser_data/feishu_projects.json'
```

Then run the synthetic `/新对话` and reply proof from Phase 3.

### Failure Triage Matrix

| Failure | Owning layer | Fix |
|---|---|---|
| `/v1/routing/feishu-projects.json` empty | AliECS doc-sync/backend | fix Bitable env/source/table sync into `managed_contacts` |
| WebDock `feishu_projects.json` still empty after 70s | WebDock routing puller/runtime env | verify `ALI_ECS_BACKEND_URL`/`BACKEND_BASE_URL`, puller logs, target path permissions |
| `/新对话` ack works but next message stays in old `/c/` | WebDock lane state/router | inspect `lane_state.json`, `clear_conversation`, `resolve_target_url(force_new=True)` |
| Feishu DM lands on `wechat:*` lane | AliECS bridge metadata | fix `build_webdock_metadata()` Feishu detection/peer stripping |
| Group no-@ gets ChatGPT reply | mention gate | stop group auto-reply, fix `mentions` parsing, verify raw event source |
| Rich reply image loses text | WebDock widget screenshot | use live screenshot first; inspect debug screenshot before changing Feishu sending |
| Feishu cannot show image attachment | OpenClaw/Feishu send path | verify `MEDIA:` token handling; fallback to direct media URL text until sender supports image |

### Formal Deploy

After hot verification is green:

1. Commit AliECS changes in AliECS repo.
2. Commit WebDock changes in WebDock repo.
3. Push according to repo rules.
4. Wait for GitHub Actions release/deploy.
5. Re-run runtime verification.

AliECS final checks:

```bash
ssh aliecs 'cd /root/AliECS && git status -sb && git log -1 --oneline'
ssh aliecs 'cd /root/AliECS && ./deploy/ecs/post-deploy-smoke.sh'
```

WebDock final checks:

```bash
ssh webdock 'cd /opt/webdock && git status -sb && git log -1 --oneline'
ssh webdock 'curl -fsS http://100.97.176.57:18000/healthz'
```

---

## Definition of Done

- `会话索引表` active/current Feishu rows appear in backend `/v1/routing/feishu-projects.json`.
- WebDock pulls the same rows into `feishu_projects.json`.
- Feishu `/新对话` clears the old Feishu conversation and the next message opens a fresh ChatGPT `/c/...` under the configured project.
- The new conversation URL is visible in `lane_state.json` and, after write-back is implemented, in the Bitable `会话索引表`.
- Private Feishu messages reply normally.
- Group messages without @bot are logged and not sent to ChatGPT.
- Group messages with @bot are logged, cleaned, sent to ChatGPT, and replied.
- Rich/widget/image ChatGPT replies sent to Feishu are readable: Markdown for text replies, PNG/media for widget/image replies.
- Local tests and runtime verification pass after formal deploy.

