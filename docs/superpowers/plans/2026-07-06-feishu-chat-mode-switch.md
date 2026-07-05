# 飞书切换 ChatGPT 对话模式 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 飞书用户发 `/模式 极速|均衡|高级` 即可按会话粘性切换 ChatGPT 网页对话模式。

**Architecture:** bridge 解析命令并短路回复，状态存内存+会话台 bitable（用户表/群表"对话模式"列），每次请求 metadata 下发 `chatgpt_mode`；WebDock 在发送前校准页面模式选择器（一致零开销，失败不阻断）。

**Tech Stack:** Python（bridge 单文件 / webdock FastAPI+patchright）、pytest、飞书 Bitable API。

**Spec:** `docs/superpowers/specs/2026-07-06-feishu-chat-mode-switch-design.md`

## Global Constraints

- 规范模式值：`fast` / `balanced` / `advanced`；中文标签：极速 / 均衡 / 高级；bitable 列名：`对话模式`。
- 模式切换任何失败**不得阻断回复**：只打日志，照常发送。
- metadata 无 `chatgpt_mode` 时 WebDock 行为与现状完全一致（微信链路零影响）。
- git add 必须显式列文件路径，禁 `-A`/`.`/`-u`；不碰工作区已有的无关未提交改动。
- AliECS 改动落在分支 `feature/feishu-chat-mode-switch` 走 PR；webdock 可直推 main，但推前必须本地全量 pytest 通过。
- webdock 测试命令：仓库根 `python -m pytest`；bridge 测试：AliECS 根 `python -m pytest tests/test_openclaw_bridge.py`。
- 不给 `feishu_command_type`/`feishu_task_type` 增加 `/模式`：消息日志"任务类型"是 bitable 单选列，新增选项值有写入失败风险；`/模式` 记录为"普通回复"即可。

---

### Task 1: webdock LaneContext 解析 chatgpt_mode

**Files:**
- Modify: `webdock/src/browser/lane_scheduler.py:54-87`（LaneContext）
- Test: `webdock/tests/test_chat_lane_scheduler.py`

**Interfaces:**
- Produces: `LaneContext.chatgpt_mode: str | None`（值 ∈ {"fast","balanced","advanced"} 或 None），Task 3 消费。

- [ ] **Step 1: 写失败测试**（追加到 `test_chat_lane_scheduler.py`）

```python
def test_lane_context_parses_chatgpt_mode():
    lane = LaneContext.from_metadata(
        {"channel": "feishu", "peer_id": "user:ou_x", "chatgpt_mode": "fast"}
    )
    assert lane.chatgpt_mode == "fast"


def test_lane_context_rejects_unknown_chatgpt_mode():
    lane = LaneContext.from_metadata(
        {"channel": "feishu", "peer_id": "user:ou_x", "chatgpt_mode": "turbo"}
    )
    assert lane.chatgpt_mode is None


def test_lane_context_chatgpt_mode_defaults_none():
    lane = LaneContext.from_metadata({"channel": "feishu", "peer_id": "user:ou_x"})
    assert lane.chatgpt_mode is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_chat_lane_scheduler.py -k chatgpt_mode -v`（webdock 根）
Expected: FAIL（`chatgpt_mode` 属性不存在 / TypeError）

- [ ] **Step 3: 最小实现**

`LaneContext` dataclass 增加字段（放在 `previous_target_url` 之后）：

```python
    # ChatGPT web mode requested by the bridge ("fast"/"balanced"/"advanced");
    # None = don't touch the mode picker (legacy behavior, WeChat unaffected).
    chatgpt_mode: str | None = None
```

模块级常量（`DEFAULT_PEER` 附近）：

```python
CHATGPT_MODES = {"fast", "balanced", "advanced"}
```

`from_metadata` 中 `previous_target_url = ...` 行后加：

```python
        mode = str(data.get("chatgpt_mode") or "").strip().lower()
        chatgpt_mode = mode if mode in CHATGPT_MODES else None
```

并在 `return cls(...)` 里加 `chatgpt_mode=chatgpt_mode,`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_chat_lane_scheduler.py -v`
Expected: 全 PASS（含存量用例——dataclass 新字段带默认值，不破坏既有构造）

- [ ] **Step 5: Commit（webdock 本地 main，先不 push）**

```bash
git add src/browser/lane_scheduler.py tests/test_chat_lane_scheduler.py
git commit -m "feat: LaneContext 解析 chatgpt_mode 元数据"
```

---

### Task 2: webdock 模式选择器校准（selectors + ensure_mode）

**Files:**
- Modify: `webdock/src/browser/selectors.py`
- Modify: `webdock/src/browser/chatgpt_page.py`（imports、模块常量、`ChatGPTPage.ask` 签名、新方法 `ensure_mode`）
- Create: `webdock/tests/test_chatgpt_mode_switch.py`

**Interfaces:**
- Consumes: 无（独立于 Task 1）。
- Produces: `ChatGPTPage.ask(message, *, timeout_seconds=None, hard_timeout_seconds=None, mode: str | None = None)`；`ChatGPTPage.ensure_mode(target: str) -> None`。Task 3 消费 `mode` 参数。

- [ ] **Step 1: 写失败测试**（新文件 `tests/test_chatgpt_mode_switch.py`）

```python
"""ensure_mode 只触碰 FakePage 暴露的这几个表面：wait_for_selector /
locator().first(hover/click/inner_text) / keyboard.press。"""
from __future__ import annotations

import asyncio

from src.browser import selectors
from src.browser.chatgpt_page import ChatGPTPage

BUTTON = selectors.MODE_PICKER_BUTTON[0]


class FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    async def hover(self):
        pass

    async def click(self):
        self._page.clicks.append(self._selector)
        self._page.on_click(self._selector)

    async def inner_text(self, timeout=None):
        return self._page.texts.get(self._selector, "")


class FakeKeyboard:
    def __init__(self, page):
        self._page = page

    async def press(self, key):
        self._page.pressed.append(key)


class FakePage:
    def __init__(self, present, texts, on_click=None):
        self.present = set(present)
        self.texts = dict(texts)
        self.clicks = []
        self.pressed = []
        self.keyboard = FakeKeyboard(self)
        self._on_click = on_click or (lambda selector: None)

    def on_click(self, selector):
        self._on_click(selector)

    async def wait_for_selector(self, selector, state="attached", timeout=None):
        if selector in self.present:
            return object()
        raise TimeoutError(selector)

    def locator(self, selector):
        return FakeLocator(self, selector)


def test_ensure_mode_skips_when_already_on_target():
    page = FakePage(present={BUTTON}, texts={BUTTON: "高级"})
    asyncio.run(ChatGPTPage(page).ensure_mode("advanced"))
    assert page.clicks == []


def test_ensure_mode_clicks_target_menu_item():
    item = f"{selectors.MODE_MENU_ITEM[0]}:has-text('极速')"

    def on_click(selector):
        if selector == item:
            page.texts[BUTTON] = "极速"

    page = FakePage(present={BUTTON, item}, texts={BUTTON: "高级"}, on_click=on_click)
    asyncio.run(ChatGPTPage(page).ensure_mode("fast"))
    assert page.clicks == [BUTTON, item]


def test_ensure_mode_missing_button_is_noop():
    page = FakePage(present=set(), texts={})
    asyncio.run(ChatGPTPage(page).ensure_mode("fast"))
    assert page.clicks == [] and page.pressed == []


def test_ensure_mode_missing_menu_item_escapes_and_continues():
    page = FakePage(present={BUTTON}, texts={BUTTON: "高级"})
    asyncio.run(ChatGPTPage(page).ensure_mode("fast"))
    assert page.clicks == [BUTTON]
    assert "Escape" in page.pressed


def test_ensure_mode_none_target_is_noop():
    page = FakePage(present={BUTTON}, texts={BUTTON: "高级"})
    asyncio.run(ChatGPTPage(page).ensure_mode(""))
    assert page.clicks == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_chatgpt_mode_switch.py -v`
Expected: FAIL（`MODE_PICKER_BUTTON` / `ensure_mode` 不存在）

- [ ] **Step 3: 实现**

`selectors.py` 在 `LOGIN_INDICATORS` 之后追加（并注册进 `SELECTOR_GROUPS`）：

```python
# ChatGPT 输入区的对话模式选择器（当前中文界面：极速/均衡/高级）。按钮文本
# 显示当前模式；菜单项在运行时按文本匹配（见 chatgpt_page.ensure_mode）。
# 以下是候选列表——Task 6 会在真机 CDP 上核实并校准；ensure_mode 对全部
# 不命中容错（模式切换是 best-effort，绝不阻断发送）。
MODE_PICKER_BUTTON = [
    "button[data-testid='model-switcher-dropdown-button']",
    "button[aria-label*='模型选择器']",
    "button[aria-label*='Model selector']",
]

MODE_MENU_ITEM = [
    "[role='menuitem']",
    "[role='option']",
]
```

`SELECTOR_GROUPS` 增加两行：`"MODE_PICKER_BUTTON": MODE_PICKER_BUTTON,` 与 `"MODE_MENU_ITEM": MODE_MENU_ITEM,`。

`chatgpt_page.py`：文件头 `import` 区加 `import logging`，模块常量区（`WIDGET_SELECTOR` 上方）加：

```python
log = logging.getLogger(__name__)

# 规范模式值 -> 选择器按文本匹配用的标签（中文界面 + 英文界面兜底；
# 实际文案由 Task 6 真机校准，错配只会走 mode_switch_failed 日志路径）。
MODE_TARGET_LABELS: dict[str, tuple[str, ...]] = {
    "fast": ("极速", "Instant"),
    "balanced": ("均衡", "Auto", "Balanced"),
    "advanced": ("高级", "Thinking", "Advanced"),
}
```

`ChatGPTPage` 新方法（放在 `ask` 之前）：

```python
    async def ensure_mode(self, target: str) -> None:
        """发送前把页面的对话模式选择器校准到 target（fast/balanced/advanced）。

        多个飞书会话串行共享同一浏览器，模式是页面级状态，所以每次发送前
        都要校准而不是切换时点一次。Best-effort：任何一步失败都只打
        mode_switch_failed 日志并返回，绝不阻断这条消息的发送。
        """
        labels = MODE_TARGET_LABELS.get((target or "").strip().lower())
        if not labels:
            return
        try:
            button = await find_first(
                self.page, selectors.MODE_PICKER_BUTTON, visible=True, timeout_ms=2000
            )
            if not button:
                log.warning("mode_switch_failed stage=button target=%s", target)
                return
            current = await self.page.locator(button).first.inner_text(timeout=1500)
            if any(label in (current or "") for label in labels):
                return
            await hover_and_click(self.page, button)
            item = None
            for label in labels:
                for base in selectors.MODE_MENU_ITEM:
                    candidate = f"{base}:has-text('{label}')"
                    if await find_first(self.page, [candidate], visible=True, timeout_ms=1200):
                        item = candidate
                        break
                if item:
                    break
            if not item:
                await self.page.keyboard.press("Escape")
                log.warning("mode_switch_failed stage=menu target=%s", target)
                return
            await hover_and_click(self.page, item)
            confirmed = await self.page.locator(button).first.inner_text(timeout=1500)
            if not any(label in (confirmed or "") for label in labels):
                log.warning(
                    "mode_switch_failed stage=verify target=%s shows=%r", target, confirmed
                )
        except Exception as exc:
            log.warning("mode_switch_failed stage=exception target=%s error=%s", target, exc)
```

`ask` 签名加 `mode: str | None = None`（`hard_timeout_seconds` 之后），并在找到输入框之后、`random_delay`/`paste_text` 之前（现 168 行处）插入：

```python
            if mode:
                await self.ensure_mode(mode)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_chatgpt_mode_switch.py -v`
Expected: 5 个用例全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/browser/selectors.py src/browser/chatgpt_page.py tests/test_chatgpt_mode_switch.py
git commit -m "feat: ChatGPTPage.ensure_mode 发送前校准对话模式选择器"
```

---

### Task 3: webdock 调度器把 lane.chatgpt_mode 传进 ask

**Files:**
- Modify: `webdock/src/browser/lane_scheduler.py:153-222`（`_default_ask` 与 `ask` 的调用点）
- Test: `webdock/tests/test_chat_lane_scheduler.py`

**Interfaces:**
- Consumes: Task 1 的 `LaneContext.chatgpt_mode`；Task 2 的 `ChatGPTPage.ask(..., mode=)`。

- [ ] **Step 1: 写失败测试**（追加到 `test_chat_lane_scheduler.py`；文件顶部已有 `FakeBrowser`，若本测试用不到可不动）

```python
def test_default_ask_forwards_mode(monkeypatch):
    captured = {}

    class FakeChatPage:
        def __init__(self, page, media_store=None, channel="wechat"):
            captured["channel"] = channel

        async def ask(self, message, *, timeout_seconds=None, hard_timeout_seconds=None, mode=None):
            captured["mode"] = mode
            return "ok", 0.1

    import src.browser.lane_scheduler as lane_scheduler_module

    monkeypatch.setattr(lane_scheduler_module, "ChatGPTPage", FakeChatPage)
    scheduler = ChatLaneScheduler(max_concurrent_chats=1)
    asyncio.run(scheduler._default_ask(object(), "hi", "feishu", mode="fast"))
    assert captured["mode"] == "fast"
    assert captured["channel"] == "feishu"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_chat_lane_scheduler.py::test_default_ask_forwards_mode -v`
Expected: FAIL（`_default_ask` 不认识 `mode` 关键字）

- [ ] **Step 3: 实现**

`_default_ask` 签名在 `hard_timeout_seconds` 后加 `mode: str | None = None`，透传：

```python
        return await ChatGPTPage(page, media_store=self._media_store, channel=channel).ask(
            message,
            timeout_seconds=timeout_seconds,
            hard_timeout_seconds=hard_timeout_seconds,
            mode=mode,
        )
```

`ask()` 中 `if self._ask_func_takes_channel:` 分支（现 207-214 行）给 `self._ask_func(...)` 调用追加 `mode=lane.chatgpt_mode,`（注入 ask_func 的 legacy 分支不动）。

- [ ] **Step 4: 全量测试确认通过**

Run: `python -m pytest`（webdock 根）
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/browser/lane_scheduler.py tests/test_chat_lane_scheduler.py
git commit -m "feat: 调度器透传 lane.chatgpt_mode 到 ChatGPTPage.ask"
```

---

### Task 4: bridge /模式 命令解析与状态存取

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`（新常量与 4 个函数，放在 `feishu_group_reply_policy`（现 1862 行）之后、`feishu_mentions_text` 之前）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Produces（Task 5 消费）：
  - `parse_feishu_mode_command(text: str) -> tuple[bool, str]`（(是否命令, 规范值或"")）
  - `feishu_chat_mode(details: dict) -> str`（规范值或""）
  - `set_feishu_chat_mode(details: dict, mode: str) -> bool`
  - 常量 `CHATGPT_MODE_LABELS`、`CHATGPT_MODE_NAMES`、`CHATGPT_MODE_FIELD`

- [ ] **Step 1: 写失败测试**（追加到 `test_openclaw_bridge.py`，沿用文件内 `load_bridge()` 惯例；peer key 全用唯一值避免模块级缓存串扰）

```python
def _mode_details(chat_type: str = "private", key: str = "ou_mode_default"):
    if chat_type == "group":
        return {
            "user_text": "",
            "metadata": {"channel": "feishu", "chat_type": "group", "peer_id": f"group:{key}"},
            "raw_metadata": {"chat_id": key},
        }
    return {
        "user_text": "",
        "metadata": {"channel": "feishu", "chat_type": "private", "peer_id": f"user:{key}"},
        "raw_metadata": {"open_id": key},
    }


def test_parse_feishu_mode_command():
    bridge = load_bridge()
    assert bridge.parse_feishu_mode_command("/模式 极速") == (True, "fast")
    assert bridge.parse_feishu_mode_command("  /模式 高级 ") == (True, "advanced")
    assert bridge.parse_feishu_mode_command("/模式均衡") == (True, "balanced")
    assert bridge.parse_feishu_mode_command("/模式") == (True, "")
    assert bridge.parse_feishu_mode_command("/模式 乱写") == (True, "")
    assert bridge.parse_feishu_mode_command("普通消息") == (False, "")
    assert bridge.parse_feishu_mode_command("/新对话") == (False, "")


def test_feishu_chat_mode_memory_only_roundtrip(monkeypatch):
    bridge = load_bridge()
    monkeypatch.delenv("FEISHU_SESSION_CONSOLE_USER_TABLE_ID", raising=False)
    details = _mode_details(key="ou_mode_mem_1")
    assert bridge.feishu_chat_mode(details) == ""
    assert bridge.set_feishu_chat_mode(details, "fast") is True
    assert bridge.feishu_chat_mode(details) == "fast"


def test_feishu_chat_mode_persists_to_group_table(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_GROUP_TABLE_ID", "tbl_group")
    ensured, updated = [], []
    monkeypatch.setattr(
        bridge, "ensure_feishu_bitable_fields", lambda table, names: ensured.append((table, tuple(names)))
    )
    monkeypatch.setattr(bridge, "upsert_feishu_group_record", lambda details: "rec_1")
    monkeypatch.setattr(
        bridge, "update_feishu_bitable_record", lambda table, rid, fields: updated.append((table, rid, fields))
    )
    details = _mode_details("group", "oc_mode_group_1")
    assert bridge.set_feishu_chat_mode(details, "advanced") is True
    assert ensured == [("tbl_group", ("对话模式",))]
    assert updated == [("tbl_group", "rec_1", {"对话模式": "高级"})]


def test_feishu_chat_mode_reads_user_table(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_USER_TABLE_ID", "tbl_user")
    monkeypatch.setattr(
        bridge,
        "find_feishu_bitable_record",
        lambda table, field, key: {"fields": {"open_id": key, "对话模式": "极速"}},
    )
    details = _mode_details(key="ou_mode_read_1")
    assert bridge.feishu_chat_mode(details) == "fast"


def test_feishu_chat_mode_keeps_memory_when_bitable_down(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_USER_TABLE_ID", "tbl_user")

    def boom(*args, **kwargs):
        raise RuntimeError("bitable down")

    monkeypatch.setattr(bridge, "find_feishu_bitable_record", boom)
    monkeypatch.setattr(bridge, "ensure_feishu_bitable_fields", lambda *a: None)
    monkeypatch.setattr(bridge, "upsert_feishu_user_record", boom)
    details = _mode_details(key="ou_mode_fail_1")
    assert bridge.set_feishu_chat_mode(details, "balanced") is True
    monkeypatch.setattr(bridge, "FEISHU_CHAT_MODE_CACHE_SECONDS", 0.0)
    assert bridge.feishu_chat_mode(details) == "balanced"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_openclaw_bridge.py -k mode -v`（AliECS 根）
Expected: FAIL（`parse_feishu_mode_command` 不存在）

- [ ] **Step 3: 实现**（`feishu_group_reply_policy` 之后插入）

```python
CHATGPT_MODE_LABELS = {"极速": "fast", "均衡": "balanced", "高级": "advanced"}
CHATGPT_MODE_NAMES = {value: key for key, value in CHATGPT_MODE_LABELS.items()}
CHATGPT_MODE_FIELD = "对话模式"
FEISHU_CHAT_MODE_CACHE_SECONDS = float(os.getenv("FEISHU_CHAT_MODE_CACHE_SECONDS", "30"))
_feishu_chat_mode_cache: dict[str, tuple[float, str]] = {}
_feishu_chat_mode_cache_lock = threading.Lock()


def parse_feishu_mode_command(text: str) -> tuple[bool, str]:
    """(是否 /模式 命令, 规范模式值)。参数缺失/非法时规范值为 ""（回用法提示）。"""
    stripped = (text or "").strip()
    if not stripped.startswith("/模式"):
        return False, ""
    argument = stripped[len("/模式"):].strip()
    return True, CHATGPT_MODE_LABELS.get(argument, "")


def feishu_mode_peer(details: dict[str, Any]) -> tuple[str, str]:
    """模式状态的归属：群聊挂群表(chat_id)，私聊挂用户表(open_id)。"""
    if feishu_is_group_message(details):
        return "group", feishu_chat_id(details)
    return "user", feishu_open_id(details)


def feishu_chat_mode(details: dict[str, Any]) -> str:
    """当前会话的粘性模式（规范值），未设置返回 ""。

    bitable 是事实源；读失败时保留内存里的旧值（退化为纯内存，符合
    "bitable 不可用只降级不阻断"的约定）。"""
    kind, key = feishu_mode_peer(details)
    if not key:
        return ""
    cache_key = f"{kind}:{key}"
    now = time.monotonic()
    with _feishu_chat_mode_cache_lock:
        cached = _feishu_chat_mode_cache.get(cache_key)
        if cached and now - cached[0] < FEISHU_CHAT_MODE_CACHE_SECONDS:
            return cached[1]
    mode = cached[1] if cached else ""
    table_id = feishu_session_console_table_id(kind)
    if table_id:
        try:
            record = find_feishu_bitable_record(
                table_id, "chat_id" if kind == "group" else "open_id", key
            )
            label = bitable_field_text(((record or {}).get("fields") or {}).get(CHATGPT_MODE_FIELD)).strip()
            mode = CHATGPT_MODE_LABELS.get(label, "")
        except Exception as exc:
            log_line(
                "feishu_chat_mode_read_failed "
                + json.dumps({"peer": cache_key, "error": str(exc)}, ensure_ascii=False, sort_keys=True)
            )
    with _feishu_chat_mode_cache_lock:
        _feishu_chat_mode_cache[cache_key] = (now, mode)
    return mode


def set_feishu_chat_mode(details: dict[str, Any], mode: str) -> bool:
    """设置会话粘性模式。内存立即生效；bitable 持久化 best-effort。"""
    kind, key = feishu_mode_peer(details)
    if not key:
        return False
    with _feishu_chat_mode_cache_lock:
        _feishu_chat_mode_cache[f"{kind}:{key}"] = (time.monotonic(), mode)
    table_id = feishu_session_console_table_id(kind)
    if not table_id:
        return True
    try:
        ensure_feishu_bitable_fields(table_id, [CHATGPT_MODE_FIELD])
        record_id = (
            upsert_feishu_group_record(details) if kind == "group" else upsert_feishu_user_record(details)
        )
        if record_id:
            update_feishu_bitable_record(table_id, record_id, {CHATGPT_MODE_FIELD: CHATGPT_MODE_NAMES[mode]})
    except Exception as exc:
        log_line(
            "feishu_chat_mode_persist_failed "
            + json.dumps({"peer": f"{kind}:{key}", "mode": mode, "error": str(exc)}, ensure_ascii=False, sort_keys=True)
        )
    return True
```

注意：`threading` 若未导入需补（该文件已有 `_feishu_group_policy_cache_lock`，`threading` 应已在 import 区，核实即可）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_openclaw_bridge.py -k mode -v`
Expected: 5 个新用例全 PASS

- [ ] **Step 5: Commit（分支 `feature/feishu-chat-mode-switch`）**

```bash
git add deploy/openclaw-bridge/openclaw_bridge.py tests/test_openclaw_bridge.py
git commit -m "feat(bridge): /模式 命令解析与会话粘性模式状态(内存+bitable)"
```

---

### Task 5: bridge 命令短路回复 + metadata 下发

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`（`build_reply` 现 2701-2704 行之后；`request_details` 现 512 行之后；新函数 `maybe_feishu_mode_command_reply` 放在 `build_reply` 之前）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: Task 4 的 `parse_feishu_mode_command` / `feishu_chat_mode` / `set_feishu_chat_mode` / `CHATGPT_MODE_NAMES`。
- Produces: 出站 webdock 请求 `metadata["chatgpt_mode"]`（Task 1 消费）。

- [ ] **Step 1: 写失败测试**

```python
def test_feishu_mode_command_switch_reply(monkeypatch):
    bridge = load_bridge()
    recorded = []
    monkeypatch.setattr(bridge, "set_feishu_chat_mode", lambda details, mode: recorded.append(mode) or True)
    details = _mode_details(key="ou_mode_cmd_1")
    details["user_text"] = "/模式 极速"
    reply = bridge.maybe_feishu_mode_command_reply(details)
    assert recorded == ["fast"]
    assert "极速" in reply


def test_feishu_mode_command_usage_reply(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_chat_mode", lambda details: "advanced")
    details = _mode_details(key="ou_mode_cmd_2")
    details["user_text"] = "/模式"
    reply = bridge.maybe_feishu_mode_command_reply(details)
    assert "高级" in reply and "用法" in reply


def test_feishu_mode_command_ignores_wechat():
    bridge = load_bridge()
    details = {
        "user_text": "/模式 极速",
        "metadata": {"channel": "wechat", "chat_type": "private", "peer_id": "user:wx_1"},
        "raw_metadata": {},
    }
    assert bridge.maybe_feishu_mode_command_reply(details) is None


def test_feishu_mode_command_normal_text_passthrough():
    bridge = load_bridge()
    details = _mode_details(key="ou_mode_cmd_3")
    details["user_text"] = "今天天气如何"
    assert bridge.maybe_feishu_mode_command_reply(details) is None


def test_build_reply_mode_command_short_circuits(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("WEB_DOCK_BASE_URL", "http://webdock.invalid/v1")
    monkeypatch.setenv("WEB_DOCK_API_TOKEN", "token")

    def no_webdock(body):
        raise AssertionError("mode command must not reach webdock")

    monkeypatch.setattr(bridge, "call_webdock", no_webdock)
    monkeypatch.setattr(bridge, "set_feishu_chat_mode", lambda details, mode: True)
    monkeypatch.setattr(bridge, "append_feishu_session_console_records_async", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "trace_chain_result", lambda *a, **k: None)
    body = {
        "metadata": {"channel": "feishu", "chat_type": "private", "peer_id": "user:ou_mode_sc_1", "open_id": "ou_mode_sc_1"},
        "messages": [{"role": "user", "content": "/模式 均衡"}],
    }
    reply = bridge.build_reply(body)
    assert "均衡" in reply


def test_request_details_carries_chatgpt_mode(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_chat_mode", lambda details: "fast")
    monkeypatch.setattr(bridge, "find_current_feishu_session_record", lambda session_key: None)
    body = {
        "metadata": {"channel": "feishu", "chat_type": "private", "peer_id": "user:ou_mode_md_1", "open_id": "ou_mode_md_1"},
        "messages": [{"role": "user", "content": "你好"}],
    }
    details = bridge.request_details(body)
    assert details["metadata"]["chatgpt_mode"] == "fast"
```

（若 `request_details` 路径上还有其他 bitable 触点导致测试外呼，按同样方式 monkeypatch 为 no-op；以实际跑测结果为准。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_openclaw_bridge.py -k "mode_command or carries_chatgpt_mode" -v`
Expected: FAIL（`maybe_feishu_mode_command_reply` 不存在）

- [ ] **Step 3: 实现**

新函数（`build_reply` 之前）：

```python
def maybe_feishu_mode_command_reply(details: dict[str, Any]) -> str | None:
    """/模式 命令的短路回复；非飞书或非命令返回 None（走正常链路）。"""
    if (details.get("metadata") or {}).get("channel") != "feishu":
        return None
    is_command, mode = parse_feishu_mode_command(str(details.get("user_text") or ""))
    if not is_command:
        return None
    if not mode:
        current = feishu_chat_mode(details)
        current_name = CHATGPT_MODE_NAMES.get(current, "默认（高级）")
        return f"当前对话模式：{current_name}\n用法：/模式 极速｜均衡｜高级"
    if not set_feishu_chat_mode(details, mode):
        return "无法识别当前会话，模式未修改。"
    return f"已切换为{CHATGPT_MODE_NAMES[mode]}模式，本会话后续回复将使用该模式。"
```

`build_reply` 里 `return NO_REPLY`（现 2704 行，`feishu_should_send_chatgpt` 门之后）与 `batched_body = maybe_batch_request(body)` 之间插入：

```python
        mode_reply = maybe_feishu_mode_command_reply(details)
        if mode_reply is not None:
            trace_chain_result(details, started, reply=mode_reply)
            append_feishu_session_console_records_async(details, mode_reply, "已回复")
            return mode_reply
```

`request_details` 的 feishu 分支（`enrich_feishu_metadata_with_session_route(...)` 行之后）插入：

```python
        chat_mode = feishu_chat_mode({"metadata": metadata, "raw_metadata": raw_metadata})
        if chat_mode:
            metadata["chatgpt_mode"] = chat_mode
```

- [ ] **Step 4: 全量 bridge 测试确认通过**

Run: `python -m pytest tests/test_openclaw_bridge.py -v`
Expected: 全 PASS（存量+新增）

- [ ] **Step 5: Commit**

```bash
git add deploy/openclaw-bridge/openclaw_bridge.py tests/test_openclaw_bridge.py
git commit -m "feat(bridge): /模式 短路回复 + chatgpt_mode 随 metadata 下发"
```

---

### Task 6: 真机探明模式选择器 DOM 并校准 selectors

**Files:**
- Modify: `webdock/src/browser/selectors.py`（校准 `MODE_PICKER_BUTTON`）
- Modify: `webdock/src/browser/chatgpt_page.py`（校准 `MODE_TARGET_LABELS` 实际文案）
- Test: `webdock/tests/test_chatgpt_mode_switch.py`（文案若变则同步）

**Interfaces:** 无新接口；只校准 Task 2 的常量。

- [ ] **Step 1: 连主力节点（webdock2）的浏览器 CDP**

参考 `AliECS/docs/fleet.md` 与既有手法（CDP `/json` + 容器内直连）。在 ChatGPT 页面执行探测 JS：

```js
[...document.querySelectorAll("button")]
  .filter(b => /极速|均衡|高级|Instant|Thinking|Auto|Balanced/i.test(b.textContent || ""))
  .map(b => ({
    text: (b.textContent || "").trim().slice(0, 60),
    testid: b.getAttribute("data-testid"),
    aria: b.getAttribute("aria-label"),
  }))
```

再点开选择器记录菜单项的容器 role/data-testid 与三个选项的准确文案。若开发机无法直达 CDP，请求用户协助或经 ECS 隧道跳转。

- [ ] **Step 2: 按真机结果校准**

- `MODE_PICKER_BUTTON`：把命中的真实 `data-testid`/aria 放到候选列表第一位（保留兜底项）。
- `MODE_TARGET_LABELS`：把三个选项的准确文案放到各元组第一位。
- `MODE_MENU_ITEM`：按真实菜单容器校准。

- [ ] **Step 3: 同步测试并全量跑**

Run: `python -m pytest`（webdock 根）
Expected: 全 PASS

- [ ] **Step 4: Commit**

```bash
git add src/browser/selectors.py src/browser/chatgpt_page.py tests/test_chatgpt_mode_switch.py
git commit -m "fix: 按真机 DOM 校准模式选择器与文案"
```

---

### Task 7: 部署 webdock

**Files:** 无代码改动；发布操作。

- [ ] **Step 1: 推 main**

webdock 根确认 `python -m pytest` 全绿后：

```bash
git push origin main
```

- [ ] **Step 2: 等 GH Actions 构建镜像**

`gh run list --repo <webdock repo>` 看到构建完成，镜像 tag = `sha-<commit 前缀>`。（⚠️ `gh run watch` 会提前报 success，以 run list 的 conclusion 为准。）

- [ ] **Step 3: 节点换镜像**

主力节点 webdock2（Win11+WSL2，命令须 `wsl -d Ubuntu-24.04-WebDock -- ...`）：`.env` 与 systemd 两处 `WEBDOCK_IMAGE` 都改成新 tag，然后 `systemctl restart webdock`。备节点 webdock1 同法（可后做）。

- [ ] **Step 4: 健康验证**

容器起来后经 ECS 探活（bridge 链路 `/v1/models` 200），随便发条微信/飞书消息确认回复正常（回归：mode 未下发时行为不变）。

---

### Task 8: 部署 bridge + 端到端真机验证

**Files:** 无代码改动；发布操作。

- [ ] **Step 1: 开 PR 并合并**

```bash
git push -u origin feature/feishu-chat-mode-switch
gh pr create --title "feat: 飞书 /模式 命令切换 ChatGPT 对话模式" --body "..." 
gh pr merge --squash
```

- [ ] **Step 2: 构建并 cutover bridge 镜像（ECS）**

bridge-cutover workflow 构建新 `V<YYYYMMDDNNN>` 镜像后，在 aliecs：

```bash
docker pull <registry>/openclaw-bridge:V<新版本>
# 改 /root/infra/server/.env 的 OPENCLAW_BRIDGE_TAG
docker rm -f openclaw-bridge   # ⚠️ 必须先删，否则容器名冲突
docker compose up -d
curl -s http://127.0.0.1:18080/v1/models   # 健康 200
```

- [ ] **Step 3: 真机端到端验证（逐条确认）**

1. 飞书私聊发 `/模式 极速` → 收到"已切换为极速模式…"确认。
2. 发一个问题 → 正常收到回复；webdock 日志无 `mode_switch_failed`；（可经 noVNC/console 看网页选择器停在"极速"）。
3. 发 `/模式` → 显示"当前对话模式：极速"。
4. 会话台用户表出现"对话模式"列且该用户行值为"极速"。
5. `docker restart openclaw-bridge` 后再发问题 → 模式仍为极速（bitable 恢复验证）。
6. 发 `/模式 高级` 切回 → 确认回复正常。
7. 群聊 @机器人 `/模式 均衡` → 确认群表写入与群会话生效。
8. 微信发一条消息 → 回复正常（回归）。

- [ ] **Step 4: 收尾**

验证全绿后按惯例更新记忆/文档；若真机发现文案或 DOM 出入，回 Task 6 循环。
