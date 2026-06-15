# Feishu Image-Dup + /新对话 Quick-Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Executor note (Codex):** You may freely `ssh aliecs` (ECS root, 47.77.176.62) and `ssh webdock` (old PC, Tailscale 100.97.176.57, passwordless sudo). If `ssh webdock` fails, on the Windows host run PowerShell `Start-Service Tailscale; tailscale up` first. **Hard rule: AliECS goes through a PR (never push main directly); CI green → you may squash-merge your own PR. webdock may push main directly.** Runtime/hot changes MUST be committed to git or the next release rebuild overwrites them. Both fixes here are in the bridge (AliECS).

**Goal:** Fix two Feishu-specific bridge bugs: (①) a single inbound image is forwarded to ChatGPT as two images, and (③) `/新对话` (and any leading trigger) is broken because OpenClaw prefixes Feishu DM text with `<sender name>: `.

**Architecture:** openclaw-bridge (ECS, `deploy/openclaw-bridge/openclaw_bridge.py`) normalizes each OpenClaw `/v1/chat/completions` request before forwarding to webdock. It (a) cleans the user text and (b) extracts inbound image parts. Both bugs are in that normalization. (Issue ② — rich markdown for Feishu — is intentionally OUT OF SCOPE for this plan; chosen approach for later: a DOM→markdown serializer in webdock keyed on `lane.channel`.)

**Tech Stack:** Python (stdlib), pytest, Docker Compose, GitHub Actions release-deploy → `ghcr.io/huozao/openclaw-bridge:<V-tag>`.

---

## Background: Confirmed Root Causes (read before coding)

Live evidence (webdock archive 2026-06-15 14:41, a Feishu image-edit DM):
- `inbound.text = "hao: /新对话 帮我把这个图片中的人物变为一对情侣\n![image]"`
- `inbound.images = 2` (the user sent ONE image — "这个图片", singular)

**③ `/新对话` broken — confirmed.** OpenClaw formats Feishu DM text as `<sender name>: <message>` (here `hao: …`). WeChat text arrives clean (e.g. `能P图嘛`). webdock's `parse_new_conversation_trigger` (webdock/src/browser/lane_routing.py) only matches text that *starts with* `/新对话`; the `hao: ` prefix defeats it. The bridge already strips the Feishu `Sender (untrusted metadata):` envelope and `[message_id: …]` line (commit 92f1d52) but NOT the `<name>: ` prefix.

**① One image → two — confirmed mechanism.** `extract_image_parts(content)` (openclaw_bridge.py:248) for a list content scans each item's `text` for `[media attached: media://inbound/<id>]` refs (resolved to data URLs via the mounted inbound dir) **and** separately appends any `image_url` part. The dedup `seen` set lives inside `extract_openclaw_media_image_parts` (one text scan) only — it does NOT dedup across the text-ref source and the image_url-part source. WeChat sends images only as text media-refs (no `image_url` parts), so it was unaffected historically; Feishu appears to send both → the same image is added twice. **Task 1 confirms the exact structure and which source is reliable for Feishu before the fix.**

## Sources To Recheck Before Execution

- `deploy/openclaw-bridge/openclaw_bridge.py`: `extract_image_parts` (248-271), `extract_openclaw_media_image_parts` (277-313), `resolve_openclaw_inbound_media` (316-335), `request_details` (406-416), `clean_user_text` (135-141), `get_last_user_metadata` (244).
- `tests/test_openclaw_bridge.py` (existing patterns + the `bridge` module reference used by current Feishu tests).
- webdock `parse_new_conversation_trigger` (webdock/src/browser/lane_routing.py) — no change; just the consumer of the cleaned text.

## File Map

- Modify: `deploy/openclaw-bridge/openclaw_bridge.py` (strip Feishu sender prefix; dedup image parts)
- Modify: `tests/test_openclaw_bridge.py` (new failing tests)
- Runtime (ECS, not git): re-cutover bridge to the new release tag

---

## Task 1: Capture the real Feishu (and WeChat) inbound image request — evidence for ①

**Files:** none (read-only on ECS).

- [ ] **Step 1: Capture the raw POST while a single Feishu image is sent**

Ask the human to send the Feishu bot ONE image with a caption (e.g. `把这张图改成卡通风`). Capture:
```bash
ssh aliecs 'timeout 90 tcpdump -i any -A -s0 "tcp port 18080 and greater 200" 2>/dev/null > /tmp/feishu_img_cap.txt; wc -l /tmp/feishu_img_cap.txt'
ssh aliecs 'sed -n "1,500p" /tmp/feishu_img_cap.txt'
```

- [ ] **Step 2: Determine the image-carrying structure and the reliable source**

From the captured `messages[].content`, answer and record here:
1. Is `content` a string or a list of parts?
2. Does the image appear as a text `[media attached: media://inbound/<id>]` ref, as an `image_url` part, or **both**? (Both → that is the duplication.)
3. If an `image_url` part exists, is its URL a `data:` URL (fetchable by webdock) or a Feishu/HTTP URL (likely NOT fetchable by webdock without auth)?
4. Do Feishu inbound images actually land in the mounted inbound dir? Check: `ssh aliecs 'ls -lt /root/.openclaw/media/inbound | head'` right after the send.

**Decision rule for the fix (used in Task 5):** keep exactly ONE part per inbound image, preferring the source that is reliably fetchable by webdock:
- If Feishu images ARE saved to the inbound dir and the text media-ref resolves → prefer the **text media-ref (inbound data URL)** and drop duplicate `image_url` parts.
- If Feishu images are NOT in the inbound dir (text-ref unresolved) → prefer the **`image_url` data part** and ignore unresolved text refs.
Record which branch applies.

---

## Task 2: Failing test — strip Feishu sender prefix so `/新对话` works (③)

**Files:** Test: `tests/test_openclaw_bridge.py`

- [ ] **Step 1: Add the test**

```python
def test_bridge_strips_feishu_sender_prefix_for_new_chat_trigger():
    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Sender (untrusted metadata):\n```json\n"
                    '{"label":"hao (ou_28d4)","id":"ou_28d4","name":"hao"}\n```\n\n'
                    "[message_id: om_z]\nhao: /新对话 你好"
                ),
            }
        ],
        "metadata": {"peer_id": "user:ou_28d4", "message_id": "om_z", "chat_type": "private"},
    }
    outbound = bridge.build_webdock_body(body)
    content = outbound["messages"][0]["content"]
    text = content if isinstance(content, str) else content[0]["text"]
    assert text.startswith("/新对话")
    assert "hao:" not in text


def test_bridge_keeps_wechat_text_untouched():
    body = {
        "messages": [{"role": "user", "content": "能P图嘛"}],
        "metadata": {"wechat_account": "default", "peer_id": "o9cq80@im.wechat", "chat_type": "private"},
    }
    outbound = bridge.build_webdock_body(body)
    assert outbound["messages"][0]["content"] == "能P图嘛"
```

- [ ] **Step 2: Run — expect the first to FAIL** (`text` is `hao: /新对话 你好`), the WeChat one to PASS:

```powershell
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py::test_bridge_strips_feishu_sender_prefix_for_new_chat_trigger tests/test_openclaw_bridge.py::test_bridge_keeps_wechat_text_untouched -v
```

- [ ] **Step 3: Commit the red test**

```bash
git checkout -b fix/feishu-image-dup-and-newchat
git add tests/test_openclaw_bridge.py
git commit -m "test(bridge): strip Feishu sender prefix for /新对话 (red)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Implement the sender-prefix strip (③)

**Files:** Modify: `deploy/openclaw-bridge/openclaw_bridge.py`

- [ ] **Step 1: Add the helper (near `_strip_lane_peer_prefix`)**

```python
def _strip_feishu_sender_prefix(text: str, raw_metadata: dict[str, Any]) -> str:
    """OpenClaw prefixes Feishu DM text with '<sender name>: '. Strip it (only when it
    matches the known sender) so leading triggers like '/新对话' work and the prompt
    isn't polluted. No-op when nothing matches (never strips arbitrary 'word: ')."""
    if not isinstance(text, str) or not text:
        return text
    names: list[str] = []
    for key in ("name", "label"):
        raw = str(raw_metadata.get(key) or "").strip()
        if raw:
            names.append(raw)
            names.append(raw.split(" (")[0].strip())  # "hao (ou_…)" -> "hao"
    for name in names:
        if not name:
            continue
        prefix = f"{name}: "
        if text.startswith(prefix):
            return text[len(prefix):].lstrip()
    return text
```

- [ ] **Step 2: Call it for Feishu in `request_details` (around line 406-416)**

```python
def request_details(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages")
    user_text = get_last_user_message(messages)
    images = get_last_user_images(messages)
    metadata = build_webdock_metadata(body)
    if metadata.get("channel") == "feishu":
        user_text = _strip_feishu_sender_prefix(user_text, get_last_user_metadata(messages))
    if not metadata.get("peer_id"):
        inherited_metadata = get_recent_lane_metadata()
        if inherited_metadata:
            inherited_metadata.update(metadata)
            metadata = inherited_metadata
    return {"request_id": uuid.uuid4().hex[:12], "user_text": user_text, "images": images, "metadata": metadata}
```

- [ ] **Step 3: Run the two tests — expect PASS**

```powershell
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py::test_bridge_strips_feishu_sender_prefix_for_new_chat_trigger tests/test_openclaw_bridge.py::test_bridge_keeps_wechat_text_untouched -v
```

- [ ] **Step 4: Commit**

```bash
git add deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "fix(bridge): strip Feishu '<name>: ' sender prefix so /新对话 works

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Failing test — one inbound image yields one part (①)

**Files:** Test: `tests/test_openclaw_bridge.py`

> Use the structure observed in Task 1. The test below encodes the most-likely case: a list content with BOTH a text media-ref and an `image_url` part for the same image. If Task 1 showed a different structure, mirror that instead, but the assertion (exactly ONE image part) stays.

- [ ] **Step 1: Add the test**

```python
def test_bridge_dedups_single_inbound_image_to_one_part(monkeypatch, tmp_path):
    # One inbound image present BOTH as a text media-ref and an image_url part.
    inbound = tmp_path / "abc.png"
    inbound.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 2048)  # valid PNG header + body
    monkeypatch.setenv("OPENCLAW_INBOUND_MEDIA_DIR", str(tmp_path))
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "改成卡通 [media attached: media://inbound/abc.png]"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,aaaa"}},
                ],
            }
        ],
        "metadata": {"peer_id": "user:ou_28d4", "message_id": "om_a", "chat_type": "private"},
    }
    images = bridge.get_last_user_images(body["messages"])
    assert len(images) == 1
```

- [ ] **Step 2: Run — expect FAIL** (currently returns 2):

```powershell
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py::test_bridge_dedups_single_inbound_image_to_one_part -v
```

- [ ] **Step 3: Commit the red test**

```bash
git add tests/test_openclaw_bridge.py
git commit -m "test(bridge): single inbound image must yield one part (red)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Implement image-part dedup (①)

**Files:** Modify: `deploy/openclaw-bridge/openclaw_bridge.py`

> Apply the Task 1 decision. Default below: when a list content yields BOTH inbound text-ref parts and direct `image_url` parts, keep the **inbound text-ref parts** (guaranteed fetchable) and drop the direct `image_url` parts. If Task 1 showed Feishu images are NOT in the inbound dir, invert: keep the `image_url` parts and skip unresolved text refs.

- [ ] **Step 1: Rewrite `extract_image_parts` (list path) to track text-ref vs image_url separately and dedup**

```python
def extract_image_parts(content: Any) -> list[dict[str, Any]]:
    """Normalize image parts to the OpenAI vision shape WebDock expects. A single
    inbound image must yield exactly ONE part: OpenClaw (Feishu) may annotate the
    same attachment both as a `[media attached: …]` text ref AND a separate image_url
    part — keep only one. Inbound text-refs resolve to data URLs from the mounted
    inbound dir and are always fetchable, so they win when both are present."""
    if isinstance(content, str):
        return extract_openclaw_media_image_parts(content)
    if not isinstance(content, list):
        return []
    text_ref_parts: list[dict[str, Any]] = []
    image_url_parts: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in content:
        if not isinstance(item, dict):
            continue
        for key in ("text", "content"):
            value = item.get(key)
            if isinstance(value, str):
                for part in extract_openclaw_media_image_parts(value):
                    url = part["image_url"]["url"]
                    if url not in seen_urls:
                        seen_urls.add(url)
                        text_ref_parts.append(part)
        image_url = item.get("image_url")
        if image_url is None:
            continue
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url, str) and url.strip() and url.strip() not in seen_urls:
            seen_urls.add(url.strip())
            image_url_parts.append({"type": "image_url", "image_url": {"url": url.strip()}})
    parts = text_ref_parts if text_ref_parts else image_url_parts
    return parts[:MAX_BRIDGE_IMAGES]
```

- [ ] **Step 2: Run the dedup test — expect PASS**

```powershell
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py::test_bridge_dedups_single_inbound_image_to_one_part -v
```

- [ ] **Step 3: Commit**

```bash
git add deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "fix(bridge): one inbound image -> one part (dedup text-ref vs image_url)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Full suite + regression

- [ ] **Step 1: Run the whole bridge test file** — expect all green (including the existing Feishu isolation tests and WeChat/image-attachment tests):

```powershell
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py -v
```

---

## Task 7: PR, merge, release build

- [ ] **Step 1:** `git push -u origin fix/feishu-image-dup-and-newchat && gh pr create --fill --base main --title "fix(bridge): Feishu image dedup + /新对话 sender-prefix strip"`
- [ ] **Step 2:** `gh pr checks --watch && gh pr merge --squash --delete-branch`
- [ ] **Step 3:** find the new tag: `git fetch --tags --prune && git for-each-ref --sort=-creatordate --format='%(creatordate:short) %(refname:short)' refs/tags | head -3` and `gh run list --workflow=release-deploy.yml -L 3`. Record as `$NEW_TAG` (newer than the currently deployed one). Confirm the run succeeded.

---

## Task 8: Re-cutover the bridge on ECS

> The running container is NOT compose-managed → plain `compose up -d` hits a name conflict; `rm -f` first.

- [ ] **Step 1:** `ssh aliecs 'docker pull ghcr.io/huozao/openclaw-bridge:'"$NEW_TAG"`
- [ ] **Step 2:** `ssh aliecs 'cd /root/infra/server && echo "OPENCLAW_BRIDGE_TAG='"$NEW_TAG"'" > .env && docker rm -f openclaw-bridge && docker compose -f compose.bridge.yml up -d'`
- [ ] **Step 3:** `ssh aliecs 'docker ps --format "{{.Names}}|{{.Image}}|{{.Status}}" | grep -i bridge; curl -fsS http://127.0.0.1:18080/v1/models >/dev/null && echo ":18080 OK"; docker inspect openclaw-bridge --format "RestartCount={{.RestartCount}}"'` — expect new tag, `:18080 OK`, RestartCount=0.

---

## Task 9: Live verification

- [ ] **Step 1 (③):** human sends Feishu DM `/新对话 你好`. Check webdock archive: the new conversation is opened and the reply is on-topic.
```bash
ssh webdock 'D=/var/log/webdock/archive/$(date -u +%Y-%m-%d).jsonl; grep ou_28d4f058 "$D" | tail -1 | python3 -m json.tool | sed -n "1,30p"'
```
Expected: `inbound.text` starts with `/新对话` (no `hao:`), `kind` reflects a new conversation or the reply addresses `你好`.

- [ ] **Step 2 (①):** human sends ONE Feishu image with a caption, and (separately) ONE WeChat image. Check archives:
```bash
ssh webdock 'D=/var/log/webdock/archive/$(date -u +%Y-%m-%d).jsonl; tail -4 "$D" | python3 -c "import sys,json
for l in sys.stdin:
    o=json.loads(l); print(o.get(\"lane\",{}).get(\"key\"), \"| imgs_in=\", o.get(\"inbound\",{}).get(\"images\"))"'
```
Expected: `imgs_in=1` for a single-image send on BOTH `feishu:…` and `wechat:…` lanes (was 2).

---

## Rollback

- Bridge: `ssh aliecs 'cd /root/infra/server && echo OPENCLAW_BRIDGE_TAG=<previous tag> > .env && docker rm -f openclaw-bridge && docker compose -f compose.bridge.yml up -d'`.
- Code: `gh pr revert` the merged PR.

## Acceptance Criteria

- New tests pass; full `tests/test_openclaw_bridge.py` green (no regression to WeChat or the existing Feishu isolation tests).
- Live: a single Feishu/WeChat image yields `imgs_in=1` (no duplicate).
- Live: Feishu `/新对话` opens a new conversation (sender prefix stripped); WeChat text unchanged.
- Fix merged to AliECS via PR; deployed tag recorded; bridge `:18080` healthy, RestartCount=0.
- (② rich markdown for Feishu remains a separate future plan — DOM→markdown serializer in webdock keyed on lane.channel.)
