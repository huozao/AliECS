# 飞书"处理中"单卡片方案 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每条飞书问题一进来就引用回复一张"⏳ 正在处理"占位卡，答案就绪后用 `PATCH /im/v1/messages/{id}` 把同一张卡就地替换成最终答案——一问一卡、不产生"提示+答案"两条消息，从而劝阻用户在等待期间连续提问。

**Architecture:** 全部改动集中在 bridge 单文件 `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`。占位与更新都由 bridge 直发飞书 API（旁路 OpenClaw，与现有 `deliver_feishu_*` 同一套路），不改 OpenClaw 插件、不改 webdock。新增一个按 lane 的在飞计数器，仅用于给占位卡选"温和/升级"两级文案。出站投递从"新发引用回复"改为"有占位则 patch，否则新发"，并加一层 finalize 兜底确保占位永远被解析（答案/错误/空返回都会落到那张卡上）。

**Tech Stack:** Python 3（标准库 `urllib`/`threading`/`json`），pytest。飞书 OpenAPI：`/im/v1/messages/{id}/reply`（发）、`/im/v1/messages/{id}`（PATCH 更新卡片，即插件的 `im.message.patch`）。

## Global Constraints

- 改动文件仅 `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`；测试加在 `AliECS/tests/test_openclaw_bridge.py`。
- 特性开关 `OPENCLAW_BRIDGE_PROCESSING_CARD` 默认 **关**（`"0"`）；关闭时 bridge 行为必须与今天逐字节一致（不发占位、不计数、出站走原路径）。
- 占位/更新一律 best-effort：无凭据、token 失败、发送/patch 失败都只 `log_line` 记录，**绝不**阻塞或改变"答案能不能送达"这件事。
- 只对"会真正送去 ChatGPT 的消息"发占位：占位插入点必须在 `feishu_should_send_chatgpt` 过滤（仅记录）、`maybe_batch_request`（非领队 `NO_REPLY`）之后、`call_webdock` 之前。
- 仅飞书渠道（`metadata.channel == "feishu"`）生效；企业微信路径不受影响。
- 不修改 `PendingBatch.merge` 的文本合并语义（已知"2s 内连发后文覆盖前文"坑，属独立议题，范围外）。
- 部署：AliECS 走 **PR**（非直推 main）；bridge 上线 = 镜像构建 + **手动 cutover**（非自动），开关默认关暗部署。
- 文案默认值（供 env 覆盖）：
  - ACK：`⏳ 正在处理你的问题（通常 20–60 秒），收到回复后再继续提问哦～`
  - REMIND：`⚠️ 上一条还在处理中，这条已排队；请等回复后再问，连续提问会让每条都变慢。`
  - EMPTY：`本次没有生成内容，请稍后重试。`

---

### Task 1: 让卡片可被更新（`update_multi`）

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py:2199`（`build_feishu_card` 的 return）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Produces: `build_feishu_card(segments, footer="")` 返回的 dict，其 `config` 含 `"update_multi": True`（飞书更新卡片的前置条件）。

- [x] **Step 1: 写失败测试**

```python
def test_build_feishu_card_marks_update_multi():
    bridge = load_bridge()
    card = bridge.build_feishu_card([("text", "hi")], footer="")
    assert card["config"]["update_multi"] is True
    assert card["config"]["wide_screen_mode"] is True
```

- [x] **Step 2: 运行验证失败**

Run: `pytest tests/test_openclaw_bridge.py::test_build_feishu_card_marks_update_multi -v`
Expected: FAIL（KeyError: 'update_multi'）

- [x] **Step 3: 最小实现**

把 `openclaw_bridge.py` 中 `build_feishu_card` 的 return 改为：
```python
    return {"config": {"wide_screen_mode": True, "update_multi": True}, "elements": elements}
```

- [x] **Step 4: 运行验证通过**

Run: `pytest tests/test_openclaw_bridge.py::test_build_feishu_card_marks_update_multi -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): mark feishu cards update_multi so they can be patched"
```

---

### Task 2: 发送卡片返回其 message_id

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py:1089-1110`（`feishu_send_interactive_message`）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `feishu_post_json(path, payload, *, auth_token, method="POST") -> dict`（返回飞书响应体，形如 `{"code":0,"data":{"message_id":"om_x"}}`）。
- Produces: `feishu_send_interactive_message(details, message_id, card, auth_token) -> str`（返回**新建消息**的 message_id；取不到返回 `""`）。行为对现有调用方向后兼容（它们忽略返回值）。

- [x] **Step 1: 写失败测试**

```python
def test_send_interactive_message_returns_created_id(monkeypatch):
    bridge = load_bridge()
    captured = {}
    def fake_post(path, payload, *, auth_token=None, method="POST"):
        captured["path"] = path
        return {"code": 0, "data": {"message_id": "om_created"}}
    monkeypatch.setattr(bridge, "feishu_post_json", fake_post)
    mid = bridge.feishu_send_interactive_message(
        {"metadata": {"channel": "feishu"}}, "om_user", {"config": {}, "elements": []}, "tok"
    )
    assert mid == "om_created"
    assert captured["path"] == "/im/v1/messages/om_user/reply"
```

- [x] **Step 2: 运行验证失败**

Run: `pytest tests/test_openclaw_bridge.py::test_send_interactive_message_returns_created_id -v`
Expected: FAIL（返回 None，assert mid == "om_created" 失败）

- [x] **Step 3: 最小实现**

把 `feishu_send_interactive_message` 改为捕获并返回响应里的 message_id（两条分支都返回）：
```python
def feishu_send_interactive_message(details: dict[str, Any], message_id: str, card: dict[str, Any], auth_token: str) -> str:
    """Deliver an interactive card as one message, replying to the user's message
    when we have its id. Returns the created message's id ("" if unavailable)."""
    content = json.dumps(card, ensure_ascii=False)
    if message_id:
        resp = feishu_post_json(
            f"/im/v1/messages/{urllib.parse.quote(message_id)}/reply",
            {"msg_type": "interactive", "content": content},
            auth_token=auth_token,
        )
        return str((resp.get("data") or {}).get("message_id") or "")
    if feishu_is_group_message(details):
        receive_id, receive_id_type = feishu_chat_id(details), "chat_id"
    else:
        receive_id, receive_id_type = feishu_open_id(details), "open_id"
    if not receive_id:
        raise RuntimeError("no Feishu receive_id for card delivery")
    resp = feishu_post_json(
        f"/im/v1/messages?receive_id_type={receive_id_type}",
        {"receive_id": receive_id, "msg_type": "interactive", "content": content},
        auth_token=auth_token,
    )
    return str((resp.get("data") or {}).get("message_id") or "")
```

- [x] **Step 4: 运行验证通过**

Run: `pytest tests/test_openclaw_bridge.py::test_send_interactive_message_returns_created_id -v`
Expected: PASS

- [x] **Step 5: 回归 + 提交**

Run: `pytest tests/test_openclaw_bridge.py -k feishu -v`
Expected: PASS（现有飞书卡片测试不受返回值变化影响）

```bash
git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): return created message_id from feishu card send"
```

---

### Task 3: 新增卡片 PATCH 更新原语

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`（在 `feishu_send_interactive_message` 之后新增函数）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `feishu_post_json(path, payload, *, auth_token, method) -> dict`。
- Produces: `feishu_patch_card(message_id, card, auth_token) -> None`——对已发出的卡片消息就地更新内容（`PATCH /im/v1/messages/{id}`，body `{"content": <card json 字符串>}`）。

- [x] **Step 1: 写失败测试**

```python
def test_feishu_patch_card_calls_patch_endpoint(monkeypatch):
    bridge = load_bridge()
    captured = {}
    def fake_post(path, payload, *, auth_token=None, method="POST"):
        captured.update(path=path, payload=payload, method=method, auth=auth_token)
        return {"code": 0, "data": {}}
    monkeypatch.setattr(bridge, "feishu_post_json", fake_post)
    bridge.feishu_patch_card("om_x", {"config": {}, "elements": []}, "tok")
    assert captured["path"] == "/im/v1/messages/om_x"
    assert captured["method"] == "PATCH"
    assert captured["auth"] == "tok"
    assert json.loads(captured["payload"]["content"]) == {"config": {}, "elements": []}
```

- [x] **Step 2: 运行验证失败**

Run: `pytest tests/test_openclaw_bridge.py::test_feishu_patch_card_calls_patch_endpoint -v`
Expected: FAIL（AttributeError: module has no attribute 'feishu_patch_card'）

- [x] **Step 3: 最小实现**

在 `feishu_send_interactive_message` 之后新增：
```python
def feishu_patch_card(message_id: str, card: dict[str, Any], auth_token: str) -> None:
    """Update an already-sent interactive card in place (the placeholder becomes the
    final answer). Requires the card to have been sent with config.update_multi=true."""
    feishu_post_json(
        f"/im/v1/messages/{urllib.parse.quote(message_id)}",
        {"content": json.dumps(card, ensure_ascii=False)},
        auth_token=auth_token,
        method="PATCH",
    )
```

- [x] **Step 4: 运行验证通过**

Run: `pytest tests/test_openclaw_bridge.py::test_feishu_patch_card_calls_patch_endpoint -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): add feishu_patch_card to update a sent card in place"
```

---

### Task 4: 统一投递 `feishu_put_card`（有占位则 patch，否则新发）并接入 deliver_*

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`（新增 `feishu_put_card`；改 `deliver_feishu_media:2329`、`deliver_feishu_text_card:2360` 两处调用）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `feishu_patch_card`（Task 3）、`feishu_send_interactive_message`（Task 2）、`feishu_message_id(details) -> str`。
- Produces: `feishu_put_card(details, card, auth_token) -> None`——若 `details["feishu_placeholder_msg_id"]` 存在则 patch 该卡；patch 抛错或无占位则退回"新发引用回复"。

- [x] **Step 1: 写失败测试**

```python
def test_feishu_put_card_patches_when_placeholder_present(monkeypatch):
    bridge = load_bridge()
    calls = []
    monkeypatch.setattr(bridge, "feishu_patch_card", lambda mid, card, tok: calls.append(("patch", mid)))
    monkeypatch.setattr(bridge, "feishu_send_interactive_message", lambda d, mid, card, tok: calls.append(("send", mid)) or "om_new")
    details = {"metadata": {"channel": "feishu", "message_id": "om_user"}, "feishu_placeholder_msg_id": "om_ph"}
    bridge.feishu_put_card(details, {"config": {}, "elements": []}, "tok")
    assert calls == [("patch", "om_ph")]

def test_feishu_put_card_sends_when_no_placeholder(monkeypatch):
    bridge = load_bridge()
    calls = []
    monkeypatch.setattr(bridge, "feishu_patch_card", lambda mid, card, tok: calls.append(("patch", mid)))
    monkeypatch.setattr(bridge, "feishu_send_interactive_message", lambda d, mid, card, tok: calls.append(("send", mid)) or "om_new")
    details = {"metadata": {"channel": "feishu", "message_id": "om_user"}}
    bridge.feishu_put_card(details, {"config": {}, "elements": []}, "tok")
    assert calls == [("send", "om_user")]

def test_feishu_put_card_falls_back_to_send_when_patch_fails(monkeypatch):
    bridge = load_bridge()
    calls = []
    def boom(mid, card, tok):
        raise RuntimeError("patch 429")
    monkeypatch.setattr(bridge, "feishu_patch_card", boom)
    monkeypatch.setattr(bridge, "feishu_send_interactive_message", lambda d, mid, card, tok: calls.append(("send", mid)) or "om_new")
    details = {"metadata": {"channel": "feishu", "message_id": "om_user"}, "feishu_placeholder_msg_id": "om_ph"}
    bridge.feishu_put_card(details, {"config": {}, "elements": []}, "tok")
    assert calls == [("send", "om_user")]
```

- [x] **Step 2: 运行验证失败**

Run: `pytest tests/test_openclaw_bridge.py -k feishu_put_card -v`
Expected: FAIL（AttributeError: feishu_put_card 不存在）

- [x] **Step 3: 最小实现**

在 `feishu_patch_card` 之后新增：
```python
def feishu_put_card(details: dict[str, Any], card: dict[str, Any], auth_token: str) -> None:
    """Deliver a card: patch the pending processing-card placeholder if one exists,
    otherwise send a fresh reply. Patch failure degrades to a fresh reply so the
    answer is never lost."""
    placeholder_id = details.get("feishu_placeholder_msg_id")
    if placeholder_id:
        try:
            feishu_patch_card(placeholder_id, card, auth_token)
            return
        except Exception as exc:
            log_line(f"feishu card patch failed, sending new reply: {exc}")
    feishu_send_interactive_message(details, feishu_message_id(details), card, auth_token)
```

然后把两处出站调用替换为 `feishu_put_card`：
- `deliver_feishu_media`（约 `:2329`）：
  ```python
          card = build_feishu_card(resolved, footer=format_card_footer(details))
          feishu_put_card(details, card, auth_token)
          return NO_REPLY
  ```
- `deliver_feishu_text_card`（约 `:2360`）：
  ```python
          card = build_feishu_card([("text", reply)], footer=footer)
          feishu_put_card(details, card, auth_token)
          return NO_REPLY
  ```

- [x] **Step 4: 运行验证通过 + 回归**

Run: `pytest tests/test_openclaw_bridge.py -k "feishu_put_card or feishu" -v`
Expected: PASS（新 3 例通过；现有 media/text card 测试仍绿——无占位时 `feishu_put_card` 等价于原 `feishu_send_interactive_message`）

- [x] **Step 5: 提交**

```bash
git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): route feishu card delivery through feishu_put_card (patch placeholder else send)"
```

---

### Task 5: 在飞计数器 + 配置/文案 + 占位卡发送器

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`（模块级状态 + 若干小函数；建议放在 `maybe_batch_request` 附近）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `feishu_app_credentials() -> tuple[str,str]`、`feishu_tenant_access_token() -> str`、`build_feishu_card`、`feishu_send_interactive_message`（Task 2）、`feishu_message_id`。
- Produces:
  - `_enter_inflight(lane_key: str) -> bool`（登记本 lane 一次在飞调用；返回 True 表示进入前已有别的在飞——即"追问"）。
  - `_exit_inflight(lane_key: str) -> None`（注销，必归零，`lane_key` 为空是 no-op）。
  - `processing_card_enabled() -> bool`、`processing_ack_text() -> str`、`processing_remind_text() -> str`、`processing_empty_fallback_text() -> str`。
  - `send_processing_card(details, text) -> str | None`（发占位卡，返回其 message_id；任何前置缺失/失败返回 None）。

- [x] **Step 1: 写失败测试**

```python
def test_inflight_counter_detects_overlap_and_resets():
    bridge = load_bridge()
    key = "feishu:om_peer"
    assert bridge._enter_inflight(key) is False   # 第一条，非 overlap
    assert bridge._enter_inflight(key) is True    # 第二条，overlap
    bridge._exit_inflight(key)
    bridge._exit_inflight(key)
    assert key not in bridge._inflight_counts      # 归零后清空
    assert bridge._enter_inflight(key) is False    # 归零后又是第一条
    bridge._exit_inflight(key)

def test_inflight_counter_empty_key_is_noop():
    bridge = load_bridge()
    assert bridge._enter_inflight("") is False
    bridge._exit_inflight("")  # 不抛异常

def test_processing_card_flag_default_off(monkeypatch):
    bridge = load_bridge()
    monkeypatch.delenv("OPENCLAW_BRIDGE_PROCESSING_CARD", raising=False)
    assert bridge.processing_card_enabled() is False
    monkeypatch.setenv("OPENCLAW_BRIDGE_PROCESSING_CARD", "1")
    assert bridge.processing_card_enabled() is True

def test_send_processing_card_returns_none_without_credentials(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("", ""))
    assert bridge.send_processing_card({"metadata": {"channel": "feishu"}}, "…") is None

def test_send_processing_card_sends_and_returns_id(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("app", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    seen = {}
    def fake_send(details, mid, card, tok):
        seen["mid"] = mid
        seen["card"] = card
        return "om_ph"
    monkeypatch.setattr(bridge, "feishu_send_interactive_message", fake_send)
    out = bridge.send_processing_card({"metadata": {"channel": "feishu", "message_id": "om_user"}}, "正在处理")
    assert out == "om_ph"
    assert seen["mid"] == "om_user"
    assert seen["card"]["elements"][0]["text"]["content"].find("正在处理") >= 0
```

- [x] **Step 2: 运行验证失败**

Run: `pytest tests/test_openclaw_bridge.py -k "inflight or processing_card or send_processing" -v`
Expected: FAIL（相关属性/函数不存在）

- [x] **Step 3: 最小实现**

新增模块级状态与函数（放在 `maybe_batch_request` 之前）：
```python
_inflight_counts: dict[str, int] = {}
_inflight_lock = Lock()

DEFAULT_PROCESSING_ACK_TEXT = "⏳ 正在处理你的问题（通常 20–60 秒），收到回复后再继续提问哦～"
DEFAULT_PROCESSING_REMIND_TEXT = "⚠️ 上一条还在处理中，这条已排队；请等回复后再问，连续提问会让每条都变慢。"
DEFAULT_PROCESSING_EMPTY_TEXT = "本次没有生成内容，请稍后重试。"


def _enter_inflight(lane_key: str) -> bool:
    if not lane_key:
        return False
    with _inflight_lock:
        prior = _inflight_counts.get(lane_key, 0)
        _inflight_counts[lane_key] = prior + 1
        return prior > 0


def _exit_inflight(lane_key: str) -> None:
    if not lane_key:
        return
    with _inflight_lock:
        n = _inflight_counts.get(lane_key, 1) - 1
        if n <= 0:
            _inflight_counts.pop(lane_key, None)
        else:
            _inflight_counts[lane_key] = n


def processing_card_enabled() -> bool:
    return os.getenv("OPENCLAW_BRIDGE_PROCESSING_CARD", "0").strip().lower() in {"1", "true", "yes", "on"}


def processing_ack_text() -> str:
    return os.getenv("OPENCLAW_BRIDGE_PROCESSING_ACK_TEXT", DEFAULT_PROCESSING_ACK_TEXT)


def processing_remind_text() -> str:
    return os.getenv("OPENCLAW_BRIDGE_PROCESSING_REMIND_TEXT", DEFAULT_PROCESSING_REMIND_TEXT)


def processing_empty_fallback_text() -> str:
    return os.getenv("OPENCLAW_BRIDGE_PROCESSING_EMPTY_TEXT", DEFAULT_PROCESSING_EMPTY_TEXT)


def send_processing_card(details: dict[str, Any], text: str) -> str | None:
    """Send a footer-less '正在处理' placeholder card as a reply to the user's message.
    Returns the placeholder message_id, or None if any prerequisite/step fails
    (best-effort: a failure here must never affect the real answer)."""
    if not feishu_app_credentials()[0]:
        return None
    try:
        auth_token = feishu_tenant_access_token()
    except Exception as exc:
        log_line(f"processing card: token error: {exc}")
        return None
    if not auth_token:
        return None
    try:
        card = build_feishu_card([("text", text)], footer="")
        return feishu_send_interactive_message(details, feishu_message_id(details), card, auth_token) or None
    except Exception as exc:
        log_line(f"processing card send failed: {exc}")
        return None
```

- [x] **Step 4: 运行验证通过**

Run: `pytest tests/test_openclaw_bridge.py -k "inflight or processing_card or send_processing" -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): add inflight counter, processing-card config + placeholder sender"
```

---

### Task 6: 接入 `build_reply`——占位发送、计数、finalize 兜底

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py:2433-2498`（`build_reply`）；新增 `finalize_placeholder`
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `lane_batch_key(metadata) -> str`、`_enter_inflight`/`_exit_inflight`、`processing_card_enabled`/`processing_ack_text`/`processing_remind_text`/`processing_empty_fallback_text`、`send_processing_card`、`feishu_patch_card`、`feishu_tenant_access_token`、`build_feishu_card`、`format_card_footer`、`NO_REPLY`、`FALLBACK_MESSAGE`。
- Produces: `finalize_placeholder(reply, details) -> str`——若占位仍未被解析（`reply != NO_REPLY` 且占位存在），把 `reply`（或空时的兜底文案）patch 进占位卡并返回 `NO_REPLY`；无占位或已解析则原样返回。`build_reply` 在开关开启+飞书时，围绕 `call_webdock` 发占位、计数、并让所有出口（答案/空/异常）都经 `finalize_placeholder`。

- [x] **Step 1: 写失败测试**

```python
def _feishu_body(text, message_id="om_user"):
    return {
        "messages": [{"role": "user", "content": text}],
        "metadata": {"channel": "feishu", "chat_type": "p2p", "open_id": "om_peer",
                     "peer_id": "om_peer", "message_id": message_id},
    }

def _enable_processing(monkeypatch):
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://127.0.0.1:11800/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")
    monkeypatch.setenv("OPENCLAW_BRIDGE_BATCH_SECONDS", "0")   # 关闭 batching, 直通
    monkeypatch.setenv("OPENCLAW_BRIDGE_PROCESSING_CARD", "1")

def test_build_reply_sends_placeholder_then_patches_answer(monkeypatch):
    bridge = load_bridge()
    _enable_processing(monkeypatch)
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("app", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    events = []
    monkeypatch.setattr(bridge, "send_processing_card",
                        lambda details, text: events.append(("placeholder", text)) or "om_ph")
    monkeypatch.setattr(bridge, "call_webdock",
                        lambda body: bridge.WebDockResult("成都今天多云 18~27℃", {}))
    monkeypatch.setattr(bridge, "feishu_patch_card",
                        lambda mid, card, tok: events.append(("patch", mid)))
    monkeypatch.setattr(bridge, "feishu_send_interactive_message",
                        lambda d, mid, card, tok: events.append(("send", mid)) or "om_x")
    out = bridge.build_reply(_feishu_body("成都天气"))
    assert out == bridge.NO_REPLY                      # 卡片已自投递
    assert ("placeholder", bridge.processing_ack_text()) in events
    assert ("patch", "om_ph") in events               # 占位被就地更新成答案
    assert not any(e[0] == "send" for e in events)    # 没有第二条新消息
    assert bridge._inflight_counts.get("feishu:om_peer") in (None, 0)

def test_build_reply_second_message_uses_remind_text(monkeypatch):
    bridge = load_bridge()
    _enable_processing(monkeypatch)
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("app", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    monkeypatch.setattr(bridge, "feishu_patch_card", lambda mid, card, tok: None)
    monkeypatch.setattr(bridge, "feishu_send_interactive_message", lambda d, mid, card, tok: "om_x")
    texts = []
    monkeypatch.setattr(bridge, "send_processing_card",
                        lambda details, text: texts.append(text) or "om_ph")
    # 第一条卡住在 webdock 里, 制造 overlap
    import threading
    gate = threading.Event()
    def slow_webdock(body):
        gate.wait(2.0)
        return bridge.WebDockResult("ans", {})
    monkeypatch.setattr(bridge, "call_webdock", slow_webdock)
    t1 = threading.Thread(target=lambda: bridge.build_reply(_feishu_body("Q1", "om_u1")))
    t1.start()
    time.sleep(0.2)                                   # 确保 t1 已进入 inflight
    monkeypatch.setattr(bridge, "call_webdock", lambda body: bridge.WebDockResult("ans2", {}))
    bridge.build_reply(_feishu_body("Q2", "om_u2"))   # overlap
    gate.set(); t1.join(3.0)
    assert bridge.processing_ack_text() in texts
    assert bridge.processing_remind_text() in texts

def test_build_reply_patches_placeholder_with_fallback_on_empty(monkeypatch):
    bridge = load_bridge()
    _enable_processing(monkeypatch)
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("app", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    monkeypatch.setattr(bridge, "send_processing_card", lambda details, text: "om_ph")
    monkeypatch.setattr(bridge, "call_webdock", lambda body: bridge.WebDockResult("", {}))
    patched = {}
    monkeypatch.setattr(bridge, "feishu_patch_card",
                        lambda mid, card, tok: patched.update(mid=mid, card=card))
    out = bridge.build_reply(_feishu_body("空返回场景"))
    assert out == bridge.NO_REPLY
    assert patched["mid"] == "om_ph"
    assert bridge.processing_empty_fallback_text() in json.dumps(patched["card"], ensure_ascii=False)

def test_build_reply_no_placeholder_when_flag_off(monkeypatch):
    bridge = load_bridge()
    _enable_processing(monkeypatch)
    monkeypatch.setenv("OPENCLAW_BRIDGE_PROCESSING_CARD", "0")   # 关
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("app", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    called = []
    monkeypatch.setattr(bridge, "send_processing_card", lambda d, t: called.append(t) or "om_ph")
    monkeypatch.setattr(bridge, "feishu_send_interactive_message", lambda d, mid, card, tok: "om_x")
    monkeypatch.setattr(bridge, "call_webdock", lambda body: bridge.WebDockResult("ans", {}))
    bridge.build_reply(_feishu_body("关开关"))
    assert called == []                                 # 开关关: 不发占位
```

> 注：若 `WebDockResult` 构造签名不同，先 `grep -n "class WebDockResult" deploy/openclaw-bridge/openclaw_bridge.py` 对齐字段（`reply`, `metadata`, 可能还有 `footer`）。

- [x] **Step 2: 运行验证失败**

Run: `pytest tests/test_openclaw_bridge.py -k "build_reply_sends_placeholder or remind_text or fallback_on_empty or no_placeholder_when_flag_off" -v`
Expected: FAIL（未接线：无占位事件 / 无 patch / 计数属性缺失）

- [x] **Step 3: 最小实现**

先新增 `finalize_placeholder`（放在 `deliver_feishu_text_card` 之后）：
```python
def finalize_placeholder(reply: str, details: dict[str, Any]) -> str:
    """Guarantee a sent processing-card placeholder is always resolved. If the
    delivery chain already patched it, ``reply`` is NO_REPLY and this is a no-op.
    Otherwise patch the placeholder with the final text (or an empty-reply fallback)
    so it never stays stuck on '正在处理'."""
    placeholder_id = details.get("feishu_placeholder_msg_id")
    if not placeholder_id or reply == NO_REPLY:
        return reply
    text = (reply or "").strip() or processing_empty_fallback_text()
    try:
        auth_token = feishu_tenant_access_token()
        card = build_feishu_card([("text", text)], footer=format_card_footer(details))
        feishu_patch_card(placeholder_id, card, auth_token)
        return NO_REPLY
    except Exception as exc:
        log_line(f"feishu placeholder finalize failed: {exc}")
        return reply
```

然后改 `build_reply` 的成功段与异常段。把从 `write_details = request_details(batched_body)` 到三个 `deliver_feishu_*` 的区段，改为围绕 `call_webdock` 加"占位 + 计数 + finalize"，异常段也经 `finalize_placeholder`。目标结构：
```python
        batched_body = maybe_batch_request(body)
        if batched_body == NO_REPLY:
            return NO_REPLY
        write_details = request_details(batched_body)
        write_details["request_id"] = details.get("request_id")
        lane_key = lane_batch_key(write_details.get("metadata") or {})
        is_overlap = _enter_inflight(lane_key)
        try:
            if write_details.get("metadata", {}).get("channel") == "feishu" and processing_card_enabled():
                text = processing_remind_text() if is_overlap else processing_ack_text()
                placeholder_id = send_processing_card(write_details, text)
                if placeholder_id:
                    write_details["feishu_placeholder_msg_id"] = placeholder_id
            result = call_webdock(batched_body)
            reply, response_metadata = unpack_webdock_result(result)
            write_details["webdock_footer"] = dict(getattr(result, "footer", None) or {})
            if response_metadata:
                write_details.setdefault("metadata", {}).update(response_metadata)
            reply = deliver_feishu_files(reply, write_details)
            reply = deliver_feishu_media(reply, write_details)
            reply = deliver_feishu_text_card(reply, write_details)
            reply = finalize_placeholder(reply, write_details)
            trace_chain_result(details, started, reply=reply)
            append_feishu_session_console_records_async(write_details, reply, "已回复")
            return reply
        finally:
            _exit_inflight(lane_key)
```
异常段（`except urllib.error.HTTPError` 与 `except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError)`）在构造好 `reply = diagnostic_message(...)` 之后、`return reply` 之前，各插入一行：
```python
        reply = finalize_placeholder(reply, write_details)
```
使占位卡在出错时也被就地更新成诊断信息，而不是残留"正在处理"。（`write_details` 在 `try` 前已赋值，异常来自 `call_webdock`，此时必已定义。为防御可在 `details` 赋值后加 `write_details = details`（占位）。）

- [x] **Step 4: 运行验证通过**

Run: `pytest tests/test_openclaw_bridge.py -k "build_reply_sends_placeholder or remind_text or fallback_on_empty or no_placeholder_when_flag_off" -v`
Expected: PASS

- [x] **Step 5: 全量回归 + 提交**

Run: `pytest tests/test_openclaw_bridge.py -v`
Expected: PASS（全部；尤其 batching、media/text card、metadata 转发等既有用例不回归）

```bash
git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): processing-card placeholder in build_reply (deter consecutive questions)"
```

---

### Task 7: 收尾——PR 与部署说明

**Files:**
- Modify: `AliECS/docs/superpowers/specs/2026-07-03-feishu-processing-card-design.md`（勾掉状态；如需要补"用户可见行为"一节）

- [x] **Step 1: 开分支、跑全量测试**

```bash
git checkout -b feat/feishu-processing-card
pytest tests/test_openclaw_bridge.py -v
```
Expected: PASS

- [x] **Step 2: 建 PR（不直推 main）**

```bash
git push -u origin feat/feishu-processing-card
gh pr create --fill --title "feat(bridge): 飞书处理中单卡片（劝阻连续提问）"
```

- [ ] **Step 3: 部署（PR 合并后，手动 cutover）**

- 构建/发布 bridge 镜像新标签（沿用既有 bridge 镜像发布流程）。
- ECS 上换标签并重建 `openclaw-bridge` 容器（`docker rm -f openclaw-bridge` 后 `compose up -d`，见既有 bridge cutover 笔记）。
- **开关保持关**（`OPENCLAW_BRIDGE_PROCESSING_CARD` 未设/为 0），先确认线上行为与今天一致。

- [ ] **Step 4: 真机灰度验证（打开开关）**

在 `openclaw-bridge` 的 env 设 `OPENCLAW_BRIDGE_PROCESSING_CARD=1` 并重建容器，真机飞书验证三场景：
1. 单条提问：出现"正在处理"卡 → 就地变成答案（只有一张卡）。
2. 连续提问：第二条卡显示 REMIND 文案；两条最终各自变成答案；无孤儿"正在处理"。
3. 图片/表格答案：占位卡就地变成带图卡；出错场景占位卡变成诊断信息、不残留。

- [ ] **Step 5: 记录结果**

把验证结论回填到 spec/PR；稳定后再考虑把开关默认置为开（单独一次改动）。

---

## Self-Review 记录

- **Spec 覆盖**：占位发送（Task 5/6）、就地 patch（Task 1-4/6）、两级文案+计数（Task 5/6）、失败降级矩阵（Task 4 patch→send、Task 6 finalize 兜底+异常段 patch）、仅记录/非领队不发占位（Task 6 插入点在过滤之后，`no_placeholder_when_flag_off` 及既有 NO_REPLY 用例守护）、开关默认关（Task 5 `processing_card_flag_default_off`、Task 6 `no_placeholder_when_flag_off`）、部署走 PR+手动 cutover（Task 7）。均有对应任务。
- **占位时机/2s batching 文本坑**：明确范围外（Global Constraints）。
- **类型一致**：`send_processing_card(details, text) -> str|None`、`feishu_put_card(details, card, auth_token)`、`feishu_patch_card(message_id, card, auth_token)`、`finalize_placeholder(reply, details) -> str`、`_enter_inflight(lane_key)->bool`/`_exit_inflight(lane_key)->None` 在各任务间一致。
- **待执行者注意**：Task 6 Step 1 提示先核对 `WebDockResult` 构造签名，避免测试夹具与实际字段不符。
