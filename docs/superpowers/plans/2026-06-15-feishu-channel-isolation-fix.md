# Feishu Channel Isolation Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Executor note (Codex):** You may freely `ssh aliecs` (ECS root, 47.77.176.62) and `ssh webdock` (old PC, Tailscale 100.97.176.57, passwordless sudo). If `ssh webdock` fails, on this Windows host first run `Start-Service Tailscale; tailscale up`. **Hard rule: AliECS changes go through a PR (never push main directly). webdock may push main directly.** Any runtime/hot change MUST end up committed to git or the next release rebuild overwrites it.

**Goal:** Make Feishu (Lark) DMs route to their own ChatGPT lane/project with clean prompts and rich formatting, fully isolated from WeChat, by fixing the openclaw-bridge so it correctly recognizes Feishu's real OpenClaw metadata shape.

**Architecture:** Two inbound chat paths share one reply engine. WeChat: phone → OpenClaw weixin plugin (ECS) → openclaw-bridge (ECS 127.0.0.1:18080) → reverse tunnel 11800 → webdock (old PC) → ChatGPT web → reply back. Feishu: Feishu app long-connection → OpenClaw feishu plugin (ECS) → **same** openclaw-bridge → webdock → ChatGPT → reply back via Feishu (rich-capable). The bridge is our normalization layer: it parses OpenClaw's per-message metadata, tags `channel`, builds a lane batch key, and forwards lane metadata to webdock, which picks the ChatGPT project/conversation per channel+peer.

**Tech Stack:** Python (openclaw-bridge, stdlib http.server), pytest, Docker Compose, GitHub Actions release-deploy (builds `ghcr.io/huozao/openclaw-bridge:<V-tag>`), webdock (Python browser automation on old PC).

---

## Background: Confirmed Root Cause (read before coding)

A live Feishu DM (`状态测试：请回复 feishu-dm-ok`) was traced end-to-end on 2026-06-15:

- **Gateway** received it over the Feishu long-connection and dispatched a reply (`replies=1`). The Feishu channel itself is fully configured and working.
- **Bridge** trace showed `batch_key="default|private|user:ou_28d4f058…"`, `peer_id="user:ou_28d4f058…"`, `wechat_account=null`, **no `channel` field** → the bridge did NOT detect Feishu.
- **webdock archive** showed `lane.key="wechat:default:private:user:ou_28d4f058…"`, `project="WeChat-default"` → the Feishu DM landed on the WeChat lane/project.

Three bridge bugs, all because the bridge was built/tested against a **synthetic** Feishu metadata shape that OpenClaw does not actually send:

1. **Channel not detected.** `_looks_like_feishu()` only fires on an explicit `channel:"feishu"` field, a separate `open_id` field, or a `message_id` starting `openclaw-feishu:`. The real OpenClaw request has **none** of these: top-level `body["metadata"]` carries `peer_id:"user:ou_<openid>"`, `message_id:"om_<id>"`, `chat_type:"private"` and no channel tag. So `channel` defaults to `wechat` (openclaw_bridge.py:454-457, 501-503).
2. **Prompt pollution.** OpenClaw prepends a metadata envelope to the user text. For WeChat it is `Conversation info (untrusted metadata):` (the bridge strips it → clean text like `能P图嘛`). For Feishu it is `Sender (untrusted metadata):` which the bridge's `OPENCLAW_METADATA_PREFIX_RE` does **not** match, so the whole `Sender (untrusted metadata): ```json{…}``` [message_id: om_…] hao: …` block is forwarded to ChatGPT (openclaw_bridge.py:21-23, 135-141).
3. **peer_id not normalized.** Feishu `peer_id` arrives as `user:ou_28d4…`; the `user:` prefix means it will never match the bare `ou_…` open_id keys the user maintains in `feishu_projects.json` (openclaw_bridge.py:460-461).

**WeChat is NOT broken.** The only WeChat error that day was a `RESPONSE_TIMEOUT` (long-think) on a correctly-routed message (it reached its configured `weixin-a` project on the new bridge image). WeChat metadata parsing/routing works on the current bridge. Do not "fix" WeChat; only guard it against regression.

**Why the cross-channel ("串频道") happens:** webdock's `LaneRouter` IS already channel-aware (separate `wechat_projects.json` / `feishu_projects.json`, `feishu:` lane keys). But because the bridge tells it `channel=wechat`, it looks up the Feishu open_id in the WeChat config, finds nothing, returns `None`, and falls back to the **default page** — shared with any other unconfigured peer → mixed ChatGPT conversation. Fixing the bridge channel detection + adding a `feishu_projects.json` mapping isolates Feishu onto its own project/conversation. Rich vs plain formatting is then automatic: ChatGPT emits markdown, OpenClaw's Feishu channel renders it richly, OpenClaw's WeChat channel sends plain text. A dedicated Feishu ChatGPT project with "use rich markdown" instructions makes the difference explicit.

## Sources To Recheck Before Execution

- Bridge source (this is exactly what the running image runs; verified identical between tag `V20260615151` and main): `deploy/openclaw-bridge/openclaw_bridge.py`
- Existing bridge tests: `tests/test_openclaw_bridge.py` (note `test_bridge_forwards_feishu_open_id_as_isolated_lane` uses the synthetic shape — keep it green).
- webdock lane logic (already channel-aware, no code change expected): `webdock/src/browser/lane_routing.py`, `webdock/src/browser/lane_scheduler.py`.
- Prior ops notes: `docs/ops/feishu-channel-verify-2026-06-14.md`, plan `docs/superpowers/plans/2026-06-11-feishu-openclaw-auto-reply.md`.
- Bridge deploy/cutover: `infra/server/compose.bridge.yml`, `infra/server/cutover.sh`.

## Prerequisite Input (from the human, before Task 7)

- A ChatGPT **"Feishu" project URL** (`https://chatgpt.com/g/g-p-…/project`) whose custom instructions favor rich markdown (headings, bold, lists, tables, code blocks). The Feishu bot user's open_id is **`ou_28d4f058cbd2a13f3fcc6fd575023e8e`**. If no dedicated project URL is available, the plan still isolates Feishu onto its own lane/conversation, but the rich-vs-plain separation will rely on ChatGPT's default markdown only.

## File Map

- Modify: `deploy/openclaw-bridge/openclaw_bridge.py` (channel detection, peer normalization, envelope stripping)
- Modify: `tests/test_openclaw_bridge.py` (new failing tests for real Feishu shape + WeChat regression guard)
- Runtime (ECS, not git): re-cutover bridge container to the new release tag
- Runtime (old PC, not git): write `feishu_projects.json` into webdock's `browser_profile_dir`, restart webdock
- Modify after proof: `docs/ops/feishu-channel-verify-2026-06-14.md` (record results), this plan's checkboxes

---

## Task 1: Capture the real OpenClaw → bridge request (evidence)

**Files:** none (read-only diagnostics on ECS).

- [ ] **Step 1: Tail the bridge while a Feishu DM is sent**

Ask the human to send one Feishu DM (e.g. `计划验证：raw-capture`). Capture the loopback/docker traffic to the bridge for ~75s:

Run on ECS:
```bash
ssh aliecs 'timeout 75 tcpdump -i any -A -s0 "tcp port 18080 and greater 120" 2>/dev/null | sed -n "1,400p"'
```

Expected: you see a `POST /v1/chat/completions` body. Confirm, in `body["metadata"]`, the exact keys for: the Feishu peer (expected `peer_id:"user:ou_…"`), `message_id:"om_…"`, `chat_type`, and **whether any `channel`/`platform`/`source` field exists** (expected: none, or a non-`feishu` value). Also confirm the in-text envelope header is exactly `Sender (untrusted metadata):`.

- [ ] **Step 2: Record findings inline in this task**

Write the observed `body["metadata"]` keys here as a comment. The fix below is robust to key variation (it scans many candidate keys + the Feishu id prefixes), but if OpenClaw sends an explicit `channel`/`platform` with a recognizable value, prefer that path. No code change in this task.

---

## Task 2: Failing tests for Feishu detection + clean prompt + WeChat guard

**Files:**
- Test: `tests/test_openclaw_bridge.py`

- [ ] **Step 1: Add three tests (follow the existing module-load pattern / `bridge` reference used by `test_bridge_forwards_feishu_open_id_as_isolated_lane`)**

```python
def test_bridge_detects_feishu_from_real_openclaw_metadata():
    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Sender (untrusted metadata):\n```json\n"
                    '{"label":"hao (ou_28d4)","id":"ou_28d4","name":"hao"}\n```\n\n'
                    "[message_id: om_x100]\nhao: 状态测试：请回复 feishu-dm-ok"
                ),
            }
        ],
        "metadata": {
            "peer_id": "user:ou_28d4",
            "message_id": "om_x100",
            "chat_type": "private",
        },
    }
    outbound = bridge.build_webdock_body(body)
    md = outbound["metadata"]
    assert md["channel"] == "feishu"
    assert md["peer_id"] == "ou_28d4"               # user: prefix stripped
    assert md["chatgpt_project"] == "Feishu"
    assert bridge.lane_batch_key(md) == "feishu:ou_28d4"
    content = outbound["messages"][0]["content"]
    assert "untrusted metadata" not in content       # envelope stripped
    assert "message_id" not in content               # [message_id: …] line stripped
    assert "状态测试：请回复 feishu-dm-ok" in content


def test_bridge_detects_feishu_group_chat_id():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"peer_id": "chat:oc_6510eb", "message_id": "om_y", "chat_type": "group"},
    }
    md = bridge.build_webdock_body(body)["metadata"]
    assert md["channel"] == "feishu"
    assert md["peer_id"] == "oc_6510eb"


def test_bridge_keeps_wechat_lane_for_real_wechat_metadata():
    body = {
        "messages": [{"role": "user", "content": "能P图嘛"}],
        "metadata": {
            "wechat_account": "default",
            "peer_id": "o9cq80whD47YZs0xR1Y9Ih8rdVnc@im.wechat",
            "chat_type": "private",
            "message_id": "123",
        },
    }
    md = bridge.build_webdock_body(body)["metadata"]
    assert "channel" not in md                        # wechat path leaves channel unset
    assert md["wechat_account"] == "default"
    assert md["peer_id"] == "o9cq80whD47YZs0xR1Y9Ih8rdVnc@im.wechat"
    assert bridge.lane_batch_key(md).startswith("default|")
```

- [ ] **Step 2: Run and verify they FAIL**

Run (PowerShell, repo root `AliECS`):
```powershell
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py::test_bridge_detects_feishu_from_real_openclaw_metadata tests/test_openclaw_bridge.py::test_bridge_detects_feishu_group_chat_id tests/test_openclaw_bridge.py::test_bridge_keeps_wechat_lane_for_real_wechat_metadata -v
```
Expected: the two Feishu tests FAIL (channel comes back `wechat`, peer keeps `user:`/`chat:` prefix, content still contains the envelope). The WeChat test should PASS already (guard).

- [ ] **Step 3: Commit the failing tests**

```bash
git checkout -b fix/feishu-channel-isolation
git add tests/test_openclaw_bridge.py
git commit -m "test(bridge): feishu detection from real OpenClaw metadata shape (red)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Implement the bridge fix

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`

- [ ] **Step 1: Generalize the metadata-envelope regex (around line 21-23)**

Replace:
```python
OPENCLAW_METADATA_PREFIX_RE = re.compile(
    r"^(?:\[[^\]\n]*UTC\]\s*)?Conversation info \(untrusted metadata\):\s*",
)
```
with:
```python
OPENCLAW_METADATA_PREFIX_RE = re.compile(
    r"^(?:\[[^\]\n]*UTC\]\s*)?(?:Conversation info|Sender) \(untrusted metadata\):\s*",
)
# A leading "[message_id: …]" line OpenClaw emits for Feishu after the metadata block.
OPENCLAW_MESSAGE_ID_LINE_RE = re.compile(r"^\s*\[message_id:[^\]\n]*\]\s*\n?", re.IGNORECASE)
```

- [ ] **Step 2: Strip the leftover `[message_id: …]` line in `clean_user_text` (around line 135-141)**

Replace the body of `clean_user_text` with:
```python
def clean_user_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    _metadata, metadata_end = parse_openclaw_metadata_prefix(text)
    cleaned = text[metadata_end:] if metadata_end else text
    cleaned = OPENCLAW_MESSAGE_ID_LINE_RE.sub("", cleaned, count=1)
    cleaned = replace_binary_file_blocks(cleaned)
    return strip_openclaw_media_helper_text(cleaned).strip()
```

- [ ] **Step 3: Add a peer-prefix stripper and broaden Feishu detection (replace `_looks_like_feishu`, around line 501-503)**

```python
FEISHU_PEER_PREFIXES = ("ou_", "oc_")  # ou_ = user open_id, oc_ = group chat id


def _strip_lane_peer_prefix(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    for prefix in ("user:", "chat:", "open_id:", "openid:"):
        if lowered.startswith(prefix):
            return text[len(prefix):]
    return text


def _looks_like_feishu(metadata: dict[str, Any]) -> bool:
    if metadata.get("open_id"):
        return True
    message_id = str(metadata.get("message_id") or "").lower()
    if message_id.startswith(("openclaw-feishu:", "openclaw-lark:", "om_")):
        return True
    for key in (
        "peer_id", "open_id", "openId", "sender_id", "user_id",
        "chat_id", "from_user_id", "conversation_id", "id",
    ):
        candidate = _strip_lane_peer_prefix(metadata.get(key)).lower()
        if candidate.startswith(FEISHU_PEER_PREFIXES):
            return True
    return False
```

- [ ] **Step 4: Normalize the Feishu peer_id (strip prefix) in `build_webdock_metadata` (around line 460-461)**

Replace:
```python
    if channel == "feishu":
        peer_id = _first_metadata_value(metadata, "peer_id", "open_id", "openId", "sender_id", "user_id", "chat_id")
```
with:
```python
    if channel == "feishu":
        peer_id = _strip_lane_peer_prefix(
            _first_metadata_value(
                metadata, "peer_id", "open_id", "openId", "sender_id", "user_id", "chat_id", "id"
            )
        )
```

- [ ] **Step 5: Run the three new tests — expect PASS**

```powershell
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py::test_bridge_detects_feishu_from_real_openclaw_metadata tests/test_openclaw_bridge.py::test_bridge_detects_feishu_group_chat_id tests/test_openclaw_bridge.py::test_bridge_keeps_wechat_lane_for_real_wechat_metadata -v
```
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "fix(bridge): detect Feishu from real OpenClaw metadata (id prefixes), strip Sender envelope, normalize open_id

Channel was defaulting to wechat because OpenClaw sends peer_id=user:ou_<id>,
message_id=om_<id> and a 'Sender (untrusted metadata):' envelope — none of the
signals the old detector looked for. Feishu DMs were landing on wechat:default.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Full bridge suite + regression guard

**Files:** none.

- [ ] **Step 1: Run the whole bridge test file**

```powershell
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py -v
```
Expected: all pass, including the pre-existing `test_bridge_forwards_feishu_open_id_as_isolated_lane` (synthetic shape) and all WeChat/metadata-cleanup tests.

- [ ] **Step 2: Run the Feishu lane routing tests too**

```powershell
$env:PYTHONPATH='.'; pytest tests/test_feishu_lane_routing.py tests/test_lane_routing.py -v
```
Expected: all pass (no change expected; webdock lane logic already channel-aware).

---

## Task 5: PR, merge, and let release build the new bridge image

**Files:** none (CI builds the image).

- [ ] **Step 1: Push the branch and open a PR**

```bash
git push -u origin fix/feishu-channel-isolation
gh pr create --fill --base main --title "fix(bridge): Feishu channel isolation (real OpenClaw metadata)"
```

- [ ] **Step 2: Wait for CI to pass, then merge**

```bash
gh pr checks --watch
gh pr merge --squash --delete-branch
```

- [ ] **Step 3: Find the new release V-tag the merge produced**

`release-deploy.yml` triggers on push to `main` and builds `ghcr.io/huozao/openclaw-bridge:<VYYYYMMDDNNN>`.
```bash
git fetch --tags --prune
git for-each-ref --sort=-creatordate --format='%(creatordate:short) %(refname:short)' refs/tags | head -5
gh run list --workflow=release-deploy.yml -L 3
```
Record the newest `V*` tag as `$NEW_TAG` (it must be newer than the currently-running `V20260615151`). Confirm the release-deploy run succeeded.

---

## Task 6: Re-cutover the bridge on the ECS to the new tag

**Files:** runtime only (`/root/infra/server/.env` on ECS — host state, not git).

> Procedure proven 2026-06-15. The existing container is NOT compose-managed, so a plain `compose up -d` hits a name conflict — you must `rm -f` first.

- [ ] **Step 1: Pre-pull the new image (safe; does not touch the running container)**

```bash
ssh aliecs 'docker pull ghcr.io/huozao/openclaw-bridge:'"$NEW_TAG"
```
Expected: `Status: Downloaded newer image …`.

- [ ] **Step 2: Pin the tag and recreate the container**

```bash
ssh aliecs 'cd /root/infra/server && echo "OPENCLAW_BRIDGE_TAG='"$NEW_TAG"'" > .env && docker rm -f openclaw-bridge && docker compose -f compose.bridge.yml up -d'
```

- [ ] **Step 3: Verify the bridge**

```bash
ssh aliecs 'docker ps --format "{{.Names}}|{{.Image}}|{{.Status}}" | grep -i bridge; curl -fsS http://127.0.0.1:18080/v1/models >/dev/null && echo ":18080 OK"; docker inspect openclaw-bridge --format "RestartCount={{.RestartCount}}"'
```
Expected: image is `…openclaw-bridge:$NEW_TAG`, `:18080 OK`, `RestartCount=0`.

---

## Task 7: Configure webdock's Feishu project mapping (old PC)

**Files:** runtime only (`feishu_projects.json` in webdock's `browser_profile_dir`).

- [ ] **Step 1: Locate webdock's browser_profile_dir**

```bash
ssh webdock 'docker inspect webdock --format "{{range .Mounts}}{{.Source}} -> {{.Destination}}\n{{end}}"; docker exec webdock printenv | grep -iE "BROWSER_PROFILE|PROFILE_DIR|ALI_ECS_BACKEND_URL" || true'
```
Identify the host path mounted to the container's browser profile dir (where `wechat_projects.json` / `lane_state.json` already live — `ssh webdock 'sudo ls -la <that path>'` to confirm).

- [ ] **Step 2: Write `feishu_projects.json` (use the human-provided Feishu project URL; falls back to isolating without a dedicated project if none given)**

```bash
ssh webdock 'sudo tee <browser_profile_dir>/feishu_projects.json >/dev/null <<JSON
{
  "lanes": {
    "ou_28d4f058cbd2a13f3fcc6fd575023e8e": {
      "name": "hao (Feishu)",
      "project_url": "<FEISHU_PROJECT_URL>"
    }
  }
}
JSON'
```
If no `<FEISHU_PROJECT_URL>` is available, skip this file for now — the bridge fix alone moves Feishu onto its own `feishu:ou_…` lane; only the dedicated rich-format project requires this mapping.

- [ ] **Step 3: Restart webdock so LaneRouter reloads the config**

`LaneRouter` loads config files once at startup (`webdock/src/browser/lane_routing.py:_load_config`), so a restart is required.
```bash
ssh webdock 'docker restart webdock && sleep 5 && curl -fsS http://127.0.0.1:18000/healthz && echo " webdock OK"'
```

---

## Task 8: Live end-to-end verification (both channels)

**Files:** none.

- [ ] **Step 1: Feishu DM proves isolation + clean prompt + rich path**

Ask the human to send a Feishu DM: `测试：用**加粗**和列表回复，确认富文本`. Then:
```bash
ssh aliecs 'docker logs --since 3m openclaw-bridge 2>&1 | grep -i bridge_request_trace | tail -3'
ssh webdock 'D=/var/log/webdock/archive/$(date -u +%Y-%m-%d).jsonl; grep ou_28d4f058 "$D" | tail -1 | python3 -m json.tool | sed -n "1,30p"'
```
Expected:
- bridge trace `batch_key` starts with `feishu:ou_28d4f058…` (NOT `default|…`).
- webdock archive `lane.key` starts with `feishu:ou_28d4f058…`, `project` is `Feishu` (if mapping set), and `inbound.text` is clean (no `Sender (untrusted metadata)` / no `[message_id: …]`).
- The human sees a richly-formatted reply in Feishu.

- [ ] **Step 2: WeChat DM proves no regression**

Ask the human to send a WeChat message from a configured account. Then:
```bash
ssh webdock 'D=/var/log/webdock/archive/$(date -u +%Y-%m-%d).jsonl; tail -3 "$D" | python3 -c "import sys,json
for l in sys.stdin:
    o=json.loads(l); ln=o.get(\"lane\",{}); print(ln.get(\"key\"), \"|\", ln.get(\"project\"), \"|\", o.get(\"status\"))"'
```
Expected: WeChat entry still on a `wechat:…` lane with its normal project and `status=ok`. (A `RESPONSE_TIMEOUT` on a hard question is the unrelated long-think issue, not this change.)

---

## Task 9: Documentation + memory

**Files:**
- Modify: `docs/ops/feishu-channel-verify-2026-06-14.md`

- [ ] **Step 1: Append a "2026-06-15 isolation fix" section** recording: the root cause (real OpenClaw metadata shape vs synthetic test), the bridge changes, `$NEW_TAG`, and the live-verified `feishu:ou_…` lane + clean prompt + rich reply. Note WeChat unaffected.

- [ ] **Step 2: Commit (docs only) via a small PR or, if trivial, note for the maintainer**

```bash
git checkout -b docs/feishu-isolation-verify
git add docs/ops/feishu-channel-verify-2026-06-14.md
git commit -m "docs(ops): record Feishu channel isolation fix + live proof

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push -u origin docs/feishu-isolation-verify && gh pr create --fill --base main
```

---

## Rollback

- **Bridge:** revert the tag — `ssh aliecs 'cd /root/infra/server && echo OPENCLAW_BRIDGE_TAG=V20260615151 > .env && docker rm -f openclaw-bridge && docker compose -f compose.bridge.yml up -d'`. (Feishu reverts to the wechat-lane behavior; WeChat unaffected.)
- **webdock mapping:** `ssh webdock 'sudo rm <browser_profile_dir>/feishu_projects.json && docker restart webdock'`.
- **Code:** `gh pr revert` the merged PR.

## Acceptance Criteria

- New bridge tests pass; full `tests/test_openclaw_bridge.py` green; existing synthetic Feishu test still green.
- Live Feishu DM: bridge `batch_key` = `feishu:ou_…`, webdock lane `feishu:ou_…`, project `Feishu` (when mapped), prompt free of the `Sender (untrusted metadata)` envelope and `[message_id: …]` line, and a richly-formatted Feishu reply.
- Live WeChat DM still routes to its `wechat:…` lane/project and replies (no regression).
- Feishu and WeChat never share a ChatGPT conversation/lane.
- The bridge fix is committed to AliECS via PR; the deployed tag is recorded; `feishu_projects.json` documented.

---

## Execution Result (2026-06-15)

Status: completed by Codex with synthetic Feishu/WeChat POSTs because true phone-side DM sending requires a human.

### Plan adjustments made during execution

- Task 1's human-triggered tcpdump was replaced with existing live evidence plus equivalent synthetic POSTs, per the task request's autonomy rule.
- The observed runtime evidence matched the diagnosed shape: `peer_id=user:ou_28d4...`, `message_id=om_...`, no usable `channel`, and `Sender (untrusted metadata):` in the text.
- The implementation kept the planned detection fields and added the `id` fallback from the Feishu text envelope.

### Code and PR

- Code PR: `https://github.com/huozao/AliECS/pull/112`
- Squash merge commit: `92f1d52d1b455a58186fd8f6d99aff2caadf72eb`
- PR checks passed: `validate`, `migration-dry-run`, `update-pr-body`
- Release workflow: `release-deploy` run `27551508399`, success

### Deployed image tag

- Bridge image tag deployed to ECS: `V20260615153`
- Image: `ghcr.io/huozao/openclaw-bridge:V20260615153`
- Digest from build log: `sha256:3e2127eb1d85c08ac441058ef9d7079a4a99db98702400c81a9bbb693011e76f`
- Note: the workflow generated a V image tag for the push build; it did not create a Git tag for this run.

### Runtime deployment

- ECS `/root/infra/server/.env`: `OPENCLAW_BRIDGE_TAG=V20260615153`
- ECS bridge check: `openclaw-bridge|ghcr.io/huozao/openclaw-bridge:V20260615153|Up`, `:18080 OK`, `RestartCount=0`
- webdock mapping: `/var/lib/webdock/browser_data/feishu_projects.json`
- Lark project URL: `https://chatgpt.com/g/g-p-6a2ffe0bac248191988612d9081dd6b1-lark/project`
- webdock health: `100.97.176.57:18000/healthz` returned `{"ok":true,"service":"webdock"}`

### Verification evidence

- Local: `PYTHONPATH=. pytest tests/test_openclaw_bridge.py -v` => `30 passed`
- Local: `PYTHONPATH=. pytest tests/test_routing_api.py -v` => `3 passed`
- webdock repo: `PYTHONPATH=. pytest tests/test_feishu_lane_routing.py tests/test_lane_routing.py -v` => `13 passed`
- Feishu synthetic token `feishu-synth-clean-220925`:
  - bridge `batch_key=feishu:ou_28d4f058cbd2a13f3fcc6fd575023e8e`
  - bridge `peer_id=ou_28d4f058cbd2a13f3fcc6fd575023e8e`
  - webdock `lane.key=feishu:ou_28d4f058cbd2a13f3fcc6fd575023e8e`
  - webdock `target_url=https://chatgpt.com/g/g-p-6a2ffe0bac248191988612d9081dd6b1-lark/c/...`
  - webdock `inbound.text=hao: 合成验证：请只回复 feishu-synth-clean-220925`
  - no `Sender (untrusted metadata)` and no `[message_id: ...]`
  - `status=ok`, outbound `feishu-synth-clean-220925`
- WeChat synthetic token `wechat-synth-ok-221113`:
  - bridge `batch_key=default|private|o9cq80whD47YZs0xR1Y9Ih8rdVnc@im.wechat`
  - webdock `lane.key=wechat:default:private:o9cq80whD47YZs0xR1Y9Ih8rdVnc_im.wechat`
  - webdock `target_url=https://chatgpt.com/g/g-p-6a1d3d0e289081918514df104d409ffd-weixin-a/c/...`
  - `status=ok`, outbound `wechat-synth-ok-221113`

### Remaining risk

- True Feishu/WeChat phone-side DM visual confirmation remains manual. The bridge and webdock path was verified with equivalent POSTs that use the real OpenClaw metadata shape.
