# 飞书 rule 表运行时控制 + 尾泡语义化 + 措辞 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把飞书运行时开关收进多维表「规则配置表」全局层、让 OpenClaw 必发的那条尾泡永远有意义（`调试尾注 ON`→链路诊断行；`OFF`→`🌿 回复完毕` 完结标记），并优化占位卡措辞。

**Architecture:** 全部改动集中在 bridge 单文件 `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`。新增一个"全局规则"读取（读 rule 表 `global-default` 记录，缓存 10min，读失败/无表/缺字段回退 env），把 `处理中卡片`/`调试尾注` 两开关的真值源从纯 env 改为"表优先、env 兜底"。build_reply 的"卡片投递成功 NO_REPLY 出口"把返回值换成一行尾注文本，OpenClaw 就把它当尾泡发（答案卡之后）。infra 仓传 tag 是可选收尾。

**Tech Stack:** Python 3 标准库（`os`/`time`/`threading`/`json`/`urllib`），pytest。飞书 bitable OpenAPI（既有封装 `find_feishu_bitable_record` / `update_feishu_bitable_record` / `create_feishu_bitable_record` / `bitable_truthy`）。

## Global Constraints

- 改动文件仅 `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`；测试加在 `AliECS/tests/test_openclaw_bridge.py`。
- 测试从 AliECS 目录跑：`cd AliECS && pytest tests/test_openclaw_bridge.py -v`。
- 一切新增读取/构造 **best-effort**：无凭据/无表/读失败/构造异常都只 `log_line`，绝不阻断答案送达；失败一律回退到"与今天一致"的行为（env 默认 / 返回 `NO_REPLY`）。
- 仅飞书渠道（`metadata.channel == "feishu"`）生效；企业微信路径不受影响。
- 尾注只替换"真发了卡"的成功出口（`reply == NO_REPLY` 且 channel==feishu）；`仅记录`/`非领队`/`新对话` 等早 `NO_REPLY` 出口与错误分支都不加尾注。
- 不改 `PendingBatch.merge`、不改卡片构建/投递、不改单卡演进机制。
- `load_bridge()`（测试夹具）每次 `exec_module` 得到全新模块，模块级缓存/常量天然隔离，无需手动清理。
- 部署：AliECS 走 **PR**（非直推 main）；bridge 上线 = 镜像构建 + **手动 cutover**。
- 文案/默认值（供 env 覆盖）：
  - ACK：`📨 已投递到 ChatGPT，正在生成（约 20–60 秒）。答案会直接更新到这张卡片，请勿重复提问 🙏`
  - REMIND：`⚠️ 上一条还在 ChatGPT 处理中，这条已排队。请等上面那张卡片出结果再问，连续提问会拖慢每一条。`
  - DONE_MARKER：`🌿 回复完毕`

---

### Task 1: rule 表全局读取 + 缓存 + 回退（`feishu_global_rule_policy`），并把两开关真值源改为"表优先 env 兜底"

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`（模块状态放在 `_inflight_lock`(约 :2028) 附近；函数放在 `processing_card_enabled`(:2058) 之前；改写 `processing_card_enabled`）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `feishu_session_console_table_id("rule") -> str`、`find_feishu_bitable_record(table_id, field_name, expected) -> dict|None`、`bitable_truthy(value) -> bool`、`FEISHU_GROUP_POLICY_CACHE_SECONDS`、`log_line`。
- Produces:
  - `_env_flag(name: str, default: bool) -> bool`
  - `feishu_global_rule_policy() -> dict[str, bool]`（键 `"处理中卡片"`、`"调试尾注"`；缓存 TTL；失败回退 env）
  - `processing_card_enabled() -> bool`（改为读 policy）
  - `debug_trailer_enabled() -> bool`（新增，读 policy）
  - `invalidate_global_rule_cache() -> None`（供 Task 2 用）

- [ ] **Step 1: 写失败测试**

```python
def test_feishu_global_rule_policy_falls_back_to_env_without_table(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_session_console_table_id", lambda kind: "")
    monkeypatch.setenv("OPENCLAW_BRIDGE_PROCESSING_CARD", "1")
    monkeypatch.setenv("OPENCLAW_BRIDGE_DEBUG_TRAILER", "0")
    policy = bridge.feishu_global_rule_policy()
    assert policy == {"处理中卡片": True, "调试尾注": False}

def test_feishu_global_rule_policy_table_value_wins(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_session_console_table_id", lambda kind: "tbl_rule")
    monkeypatch.setenv("OPENCLAW_BRIDGE_PROCESSING_CARD", "0")   # env says off...
    monkeypatch.setattr(bridge, "find_feishu_bitable_record",
                        lambda t, f, e: {"fields": {"处理中卡片": True, "调试尾注": False}})
    policy = bridge.feishu_global_rule_policy()
    assert policy["处理中卡片"] is True   # ...table wins
    assert policy["调试尾注"] is False

def test_feishu_global_rule_policy_read_failure_falls_back(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_session_console_table_id", lambda kind: "tbl_rule")
    monkeypatch.setenv("OPENCLAW_BRIDGE_DEBUG_TRAILER", "1")
    def boom(t, f, e):
        raise RuntimeError("bitable down")
    monkeypatch.setattr(bridge, "find_feishu_bitable_record", boom)
    assert bridge.feishu_global_rule_policy()["调试尾注"] is True

def test_feishu_global_rule_policy_caches(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_session_console_table_id", lambda kind: "tbl_rule")
    calls = []
    monkeypatch.setattr(bridge, "find_feishu_bitable_record",
                        lambda t, f, e: calls.append(1) or {"fields": {}})
    bridge.feishu_global_rule_policy()
    bridge.feishu_global_rule_policy()
    assert len(calls) == 1   # second call served from cache

def test_processing_card_and_debug_trailer_reflect_policy(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_global_rule_policy",
                        lambda: {"处理中卡片": False, "调试尾注": True})
    assert bridge.processing_card_enabled() is False
    assert bridge.debug_trailer_enabled() is True
```

- [ ] **Step 2: 运行验证失败**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "global_rule_policy or reflect_policy" -v`
Expected: FAIL（AttributeError: `feishu_global_rule_policy` / `debug_trailer_enabled` 不存在）

- [ ] **Step 3: 最小实现**

在模块状态区（`_inflight_lock = Lock()` 之后，约 :2028）新增：
```python
_feishu_global_rule_cache: dict[str, tuple[float, dict[str, bool]]] = {}
_feishu_global_rule_cache_lock = Lock()
```

在 `processing_card_enabled`（:2058）之前新增：
```python
def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def feishu_global_rule_policy() -> dict[str, bool]:
    """Global feishu switches, sourced from the rule bitable's ``global-default`` row
    with a short TTL cache. Any failure (no table / no creds / read error / missing
    field) falls back to env so a broken or absent table never changes behavior."""
    env_defaults = {
        "处理中卡片": _env_flag("OPENCLAW_BRIDGE_PROCESSING_CARD", False),
        "调试尾注": _env_flag("OPENCLAW_BRIDGE_DEBUG_TRAILER", True),
    }
    now = time.monotonic()
    with _feishu_global_rule_cache_lock:
        cached = _feishu_global_rule_cache.get("value")
        if cached and now - cached[0] < FEISHU_GROUP_POLICY_CACHE_SECONDS:
            return dict(cached[1])
    result = dict(env_defaults)
    table_id = feishu_session_console_table_id("rule")
    if table_id:
        try:
            record = find_feishu_bitable_record(table_id, "规则编号", "global-default")
            fields = (record or {}).get("fields") or {}
            for key in ("处理中卡片", "调试尾注"):
                if key in fields:
                    result[key] = bitable_truthy(fields.get(key))
        except Exception as exc:
            log_line(
                "feishu_global_rule_read_failed "
                + json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True)
            )
    with _feishu_global_rule_cache_lock:
        _feishu_global_rule_cache["value"] = (now, dict(result))
    return result


def invalidate_global_rule_cache() -> None:
    with _feishu_global_rule_cache_lock:
        _feishu_global_rule_cache.clear()
```

把现有 `processing_card_enabled`（:2058-2059）整体替换为：
```python
def processing_card_enabled() -> bool:
    return bool(feishu_global_rule_policy().get("处理中卡片"))


def debug_trailer_enabled() -> bool:
    return bool(feishu_global_rule_policy().get("调试尾注"))
```

- [ ] **Step 4: 运行验证通过**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "global_rule_policy or reflect_policy" -v`
Expected: PASS

- [ ] **Step 5: 回归 + 提交**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "processing or placeholder or feishu" -v`
Expected: PASS（既有占位卡用例：注意它们里若直接 `monkeypatch.setenv("OPENCLAW_BRIDGE_PROCESSING_CARD","1")` 期望开启——现在 `processing_card_enabled` 经 `feishu_global_rule_policy`，无 rule 表时回退该 env，等价，应仍绿。若个别用例因新增一次 bitable 扫描而变化，改为 `monkeypatch.setattr(bridge, "feishu_global_rule_policy", lambda: {...})`。）

```bash
cd AliECS && git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): global feishu switches from rule bitable (table-first, env fallback)"
```

---

### Task 2: `/admin/invalidate` 顺带清 global 规则缓存

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py:2748-2786`（`_handle_invalidate_feishu_group_policy`）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `invalidate_global_rule_cache`（Task 1）、`_feishu_global_rule_cache`。

- [ ] **Step 1: 写失败测试**

```python
def test_invalidate_global_rule_cache_clears(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_session_console_table_id", lambda kind: "tbl_rule")
    monkeypatch.setattr(bridge, "find_feishu_bitable_record", lambda t, f, e: {"fields": {}})
    bridge.feishu_global_rule_policy()                      # populate cache
    assert "value" in bridge._feishu_global_rule_cache
    bridge.invalidate_global_rule_cache()
    assert bridge._feishu_global_rule_cache == {}
```

- [ ] **Step 2: 运行验证失败**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py::test_invalidate_global_rule_cache_clears -v`
Expected: 若 Task 1 已实现 `invalidate_global_rule_cache`，此单测其实会 PASS；本 Task 的实质是把它接进 HTTP 端点。先跑确认 helper 正常，再改端点。

- [ ] **Step 3: 最小实现（接进端点）**

在 `_handle_invalidate_feishu_group_policy` 里，`return self._json(200, {"ok": True, "cleared": cleared})`（:2786）之前插入一行清全局缓存：
```python
        invalidate_global_rule_cache()
        return self._json(200, {"ok": True, "cleared": cleared})
```

- [ ] **Step 4: 运行验证通过**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py::test_invalidate_global_rule_cache_clears -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd AliECS && git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): invalidate endpoint also clears global rule cache"
```

---

### Task 3: `ensure_feishu_default_rule_record` 幂等补两字段

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py:1290-1311`（`ensure_feishu_default_rule_record`）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `find_feishu_bitable_record`、`create_feishu_bitable_record`、`update_feishu_bitable_record`、`bitable_created_record_id`、`log_line`。

- [ ] **Step 1: 写失败测试**

```python
def test_ensure_rule_record_creates_with_new_fields(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_session_console_table_id", lambda kind: "tbl_rule")
    monkeypatch.setattr(bridge, "find_feishu_bitable_record", lambda t, f, e: None)
    captured = {}
    monkeypatch.setattr(bridge, "create_feishu_bitable_record",
                        lambda t, fields: captured.update(fields=fields) or {"data": {"record": {"record_id": "r1"}}})
    monkeypatch.setattr(bridge, "bitable_created_record_id", lambda r: "r1")
    bridge.ensure_feishu_default_rule_record()
    assert captured["fields"]["处理中卡片"] is True
    assert captured["fields"]["调试尾注"] is True

def test_ensure_rule_record_backfills_missing_fields(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_session_console_table_id", lambda kind: "tbl_rule")
    monkeypatch.setattr(bridge, "find_feishu_bitable_record",
                        lambda t, f, e: {"record_id": "r1", "fields": {"规则编号": "global-default"}})
    updated = {}
    monkeypatch.setattr(bridge, "update_feishu_bitable_record",
                        lambda t, rid, fields: updated.update(rid=rid, fields=fields))
    bridge.ensure_feishu_default_rule_record()
    assert updated["rid"] == "r1"
    assert updated["fields"] == {"处理中卡片": True, "调试尾注": True}

def test_ensure_rule_record_no_update_when_present(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "feishu_session_console_table_id", lambda kind: "tbl_rule")
    monkeypatch.setattr(bridge, "find_feishu_bitable_record",
                        lambda t, f, e: {"record_id": "r1", "fields": {"处理中卡片": False, "调试尾注": True}})
    called = []
    monkeypatch.setattr(bridge, "update_feishu_bitable_record",
                        lambda t, rid, fields: called.append(1))
    bridge.ensure_feishu_default_rule_record()
    assert called == []   # both fields already present -> no backfill (don't overwrite)
```

- [ ] **Step 2: 运行验证失败**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "ensure_rule_record" -v`
Expected: FAIL（新字段不在 create fields；已存在分支不会 update）

- [ ] **Step 3: 最小实现**

把 `ensure_feishu_default_rule_record`（:1290-1311）整体替换为：
```python
def ensure_feishu_default_rule_record() -> str:
    table_id = feishu_session_console_table_id("rule")
    if not table_id:
        return ""
    existing = find_feishu_bitable_record(table_id, "规则编号", "global-default")
    if existing:
        record_id = str(existing.get("record_id") or "")
        current = existing.get("fields") or {}
        missing = {k: True for k in ("处理中卡片", "调试尾注") if k not in current}
        if missing and record_id:
            try:
                update_feishu_bitable_record(table_id, record_id, missing)
            except Exception as exc:
                log_line(f"ensure_rule_record backfill failed: {exc}")
        return record_id
    fields = {
        "规则编号": "global-default",
        "规则名称": "默认飞书会话规则",
        "规则对象类型": "全局",
        "是否启用": True,
        "是否记录全量消息": True,
        "回复模式": "回复所有",
        "是否允许图片": True,
        "是否允许文件": True,
        "是否需要审核": False,
        "每日最大请求数": 0,
        "敏感群标记": False,
        "处理中卡片": True,
        "调试尾注": True,
        "备注": "openclaw-bridge 自动维护",
    }
    return bitable_created_record_id(create_feishu_bitable_record(table_id, fields))
```

- [ ] **Step 4: 运行验证通过**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "ensure_rule_record" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd AliECS && git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): rule record ensures 处理中卡片/调试尾注 fields (idempotent backfill)"
```

---

### Task 4: 尾注构造器 `build_feishu_trailer` + 完结标记

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py`（常量与函数放在 `lane_batch_key`(:2095) 之后 / `maybe_batch_request` 之前）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `debug_trailer_enabled`（Task 1）、`lane_batch_key`、`_inflight_counts`、`webdock_timeout`、`log_line`。
- Produces:
  - `DEFAULT_DONE_MARKER = "🌿 回复完毕"`、`done_marker_text() -> str`
  - `build_feishu_trailer(details: dict[str, Any]) -> str`

- [ ] **Step 1: 写失败测试**

```python
def test_build_feishu_trailer_done_marker_when_off(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "debug_trailer_enabled", lambda: False)
    assert bridge.build_feishu_trailer({"metadata": {"channel": "feishu"}}) == "🌿 回复完毕"

def test_build_feishu_trailer_done_marker_env_override(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "debug_trailer_enabled", lambda: False)
    monkeypatch.setenv("OPENCLAW_BRIDGE_DONE_MARKER", "· 已送达 ·")
    assert bridge.build_feishu_trailer({"metadata": {"channel": "feishu"}}) == "· 已送达 ·"

def test_build_feishu_trailer_diagnostic_when_on(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "debug_trailer_enabled", lambda: True)
    monkeypatch.setenv("OPENCLAW_BRIDGE_TAG", "V20260703218")
    monkeypatch.setenv("WEB_DOCK_MODEL", "browser-chatgpt")
    details = {
        "request_id": "8da08f41",
        "feishu_placeholder_msg_id": "om_ph",
        "metadata": {"channel": "feishu", "peer_id": "oc_b39",
                     "chatgpt_conversation_url": "https://chatgpt.com/g/g-p/c/abc123"},
    }
    out = bridge.build_feishu_trailer(details)
    assert out.startswith("🔧")
    assert "bridge=V20260703218" in out
    assert "req=8da08f41" in out
    assert "conv=abc123" in out          # tail segment only
    assert "model=browser-chatgpt" in out
    assert "patched=yes" in out
    assert "lane=feishu:oc_b39" in out

def test_build_feishu_trailer_tag_unknown_when_missing(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setattr(bridge, "debug_trailer_enabled", lambda: True)
    monkeypatch.delenv("OPENCLAW_BRIDGE_TAG", raising=False)
    out = bridge.build_feishu_trailer({"request_id": "r", "metadata": {"channel": "feishu"}})
    assert "bridge=unknown" in out
    assert "patched=no" in out           # no placeholder id
```

- [ ] **Step 2: 运行验证失败**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "build_feishu_trailer" -v`
Expected: FAIL（`build_feishu_trailer` 不存在）

- [ ] **Step 3: 最小实现**

在 `lane_batch_key`（:2095-2107）之后新增：
```python
DEFAULT_DONE_MARKER = "🌿 回复完毕"


def done_marker_text() -> str:
    return os.getenv("OPENCLAW_BRIDGE_DONE_MARKER", DEFAULT_DONE_MARKER)


def build_feishu_trailer(details: dict[str, Any]) -> str:
    """Content for OpenClaw's mandatory final-reply bubble (posted after the bridge's
    card). Debug-trailer ON -> a one-line link diagnostic; OFF -> a calm done marker.
    Never raises: any failure degrades to the done marker."""
    if not debug_trailer_enabled():
        return done_marker_text()
    try:
        metadata = details.get("metadata") or {}
        lane = lane_batch_key(metadata)
        busy = _inflight_counts.get(lane, 0)
        conv = str(metadata.get("chatgpt_conversation_url") or "")
        conv_tail = conv.rsplit("/", 1)[-1] if conv else "-"
        req = str(details.get("request_id") or "-")
        tag = os.getenv("OPENCLAW_BRIDGE_TAG") or "unknown"
        patched = "yes" if details.get("feishu_placeholder_msg_id") else "no"
        model = os.getenv("WEB_DOCK_MODEL", "browser-chatgpt")
        return (
            f"🔧 bridge={tag} req={req} conv={conv_tail} | "
            f"busy={busy} lane={lane or '-'} | "
            f"model={model} timeout={webdock_timeout()}s patched={patched}"
        )
    except Exception as exc:
        log_line(f"feishu trailer build failed: {exc}")
        return done_marker_text()
```

- [ ] **Step 4: 运行验证通过**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "build_feishu_trailer" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd AliECS && git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): build_feishu_trailer (diagnostic line when debug on, done marker when off)"
```

---

### Task 5: `build_reply` 成功出口把 NO_REPLY 换成尾注

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py:2584-2585`（`build_reply` 成功段，`finalize_placeholder` 之后、`trace_chain_result` 之前）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Consumes: `build_feishu_trailer`（Task 4）、`NO_REPLY`。
- Produces: `build_reply` 在飞书+发卡成功（`reply == NO_REPLY`）时返回尾注文本；其余出口不变。

> 注：复用现有测试夹具 `_feishu_body(text, message_id="om_user")` 与 `_enable_processing(monkeypatch)`（已在本测试文件中，见占位卡用例）。

- [ ] **Step 1: 写失败测试**

```python
def test_build_reply_replaces_no_reply_with_trailer_on_feishu(monkeypatch):
    bridge = load_bridge()
    _enable_processing(monkeypatch)
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("app", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    monkeypatch.setattr(bridge, "send_processing_card", lambda d, t: "om_ph")
    monkeypatch.setattr(bridge, "call_webdock", lambda body: bridge.WebDockResult("答案", {}))
    monkeypatch.setattr(bridge, "feishu_patch_card", lambda mid, card, tok: None)  # patch ok -> NO_REPLY
    monkeypatch.setattr(bridge, "build_feishu_trailer", lambda details: "TRAILER")
    out = bridge.build_reply(_feishu_body("天气"))
    assert out == "TRAILER"              # NO_REPLY success exit replaced by trailer

def test_build_reply_no_trailer_when_reply_is_text(monkeypatch):
    bridge = load_bridge()
    _enable_processing(monkeypatch)
    monkeypatch.setattr(bridge, "feishu_app_credentials", lambda: ("app", "sec"))
    monkeypatch.setattr(bridge, "feishu_tenant_access_token", lambda: "tok")
    monkeypatch.setattr(bridge, "send_processing_card", lambda d, t: None)   # no placeholder
    monkeypatch.setattr(bridge, "call_webdock", lambda body: bridge.WebDockResult("答案", {}))
    # No footer -> deliver_feishu_text_card returns the raw reply (not NO_REPLY)
    monkeypatch.setattr(bridge, "format_card_footer", lambda details: "")
    called = []
    monkeypatch.setattr(bridge, "build_feishu_trailer", lambda details: called.append(1) or "TRAILER")
    out = bridge.build_reply(_feishu_body("天气"))
    assert out == "答案"                 # text reply passes through
    assert called == []                  # trailer NOT applied when reply != NO_REPLY
```

- [ ] **Step 2: 运行验证失败**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "replaces_no_reply_with_trailer or no_trailer_when_reply_is_text" -v`
Expected: FAIL（第一例返回 `NO_REPLY` 而非 `"TRAILER"`）

- [ ] **Step 3: 最小实现**

在 `build_reply` 成功段（:2584）`reply = finalize_placeholder(reply, write_details)` 之后、`trace_chain_result(...)` 之前插入：
```python
            reply = finalize_placeholder(reply, write_details)
            if (write_details.get("metadata") or {}).get("channel") == "feishu" and reply == NO_REPLY:
                reply = build_feishu_trailer(write_details)
            trace_chain_result(details, started, reply=reply)
```

- [ ] **Step 4: 运行验证通过**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "replaces_no_reply_with_trailer or no_trailer_when_reply_is_text" -v`
Expected: PASS

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -v`
Expected: PASS（尤其占位卡 `build_reply_sends_placeholder_then_patches_answer` 现在返回尾注而非 `NO_REPLY`——若该既有用例断言 `out == NO_REPLY`，需按新语义更新为断言尾注文本或 mock `build_feishu_trailer`；这是本 Task 预期内的既有用例适配，不是回归。）

```bash
cd AliECS && git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): feishu NO_REPLY success exit emits trailer (diagnostic/done marker)"
```

---

### Task 6: 占位卡措辞更新

**Files:**
- Modify: `AliECS/deploy/openclaw-bridge/openclaw_bridge.py:2030-2031`（`DEFAULT_PROCESSING_ACK_TEXT` / `DEFAULT_PROCESSING_REMIND_TEXT`）
- Test: `AliECS/tests/test_openclaw_bridge.py`

**Interfaces:**
- Produces: `processing_ack_text()` / `processing_remind_text()` 的新默认值（env 覆盖行为不变）。

- [ ] **Step 1: 写失败测试**

```python
def test_processing_ack_text_new_default(monkeypatch):
    bridge = load_bridge()
    monkeypatch.delenv("OPENCLAW_BRIDGE_PROCESSING_ACK_TEXT", raising=False)
    text = bridge.processing_ack_text()
    assert "已投递到 ChatGPT" in text
    assert "请勿重复提问" in text

def test_processing_remind_text_new_default(monkeypatch):
    bridge = load_bridge()
    monkeypatch.delenv("OPENCLAW_BRIDGE_PROCESSING_REMIND_TEXT", raising=False)
    assert "已排队" in bridge.processing_remind_text()

def test_processing_ack_text_env_override_still_wins(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("OPENCLAW_BRIDGE_PROCESSING_ACK_TEXT", "自定义")
    assert bridge.processing_ack_text() == "自定义"
```

- [ ] **Step 2: 运行验证失败**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "processing_ack_text or processing_remind_text" -v`
Expected: FAIL（旧默认文案里没有"已投递到 ChatGPT"/"已排队"）

- [ ] **Step 3: 最小实现**

把常量（:2030-2031）替换为：
```python
DEFAULT_PROCESSING_ACK_TEXT = "📨 已投递到 ChatGPT，正在生成（约 20–60 秒）。答案会直接更新到这张卡片，请勿重复提问 🙏"
DEFAULT_PROCESSING_REMIND_TEXT = "⚠️ 上一条还在 ChatGPT 处理中，这条已排队。请等上面那张卡片出结果再问，连续提问会拖慢每一条。"
```

- [ ] **Step 4: 运行验证通过**

Run: `cd AliECS && pytest tests/test_openclaw_bridge.py -k "processing_ack_text or processing_remind_text" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd AliECS && git add tests/test_openclaw_bridge.py deploy/openclaw-bridge/openclaw_bridge.py
git commit -m "feat(bridge): update placeholder ack/remind copy (delivered-to-ChatGPT wording)"
```

---

### Task 7: 收尾——PR、部署、infra 仓传 tag（可选）、真机验证

**Files:**
- 无代码；文档 `AliECS/docs/superpowers/specs/2026-07-04-feishu-rule-control-and-trailer-design.md`（勾状态）

- [ ] **Step 1: 全量测试 + 建 PR**

```bash
cd AliECS && pytest tests/test_openclaw_bridge.py -v
git push -u origin feat/feishu-rule-control-and-trailer
gh pr create --fill --title "feat(bridge): 飞书 rule 表运行时控制 + 尾泡语义化 + 措辞"
```
Expected: 全绿；PR 建成。

- [ ] **Step 2: 合并 + bridge 镜像重建 + 手动 cutover**

- PR 合并进 main → release-deploy 构建新 `openclaw-bridge:VYYYYMMDDNNN`（从 resolve-release 日志取精确 tag，别口算）。
- `gh workflow run bridge-cutover.yml -f bridge_tag=<新tag>`；盯 :18080 健康检查通过。

- [ ] **Step 3: infra 仓传 tag（可选）**

在 **infra 仓** `infra/server/compose.bridge.yml` 的 `environment:` 块加：
```yaml
      OPENCLAW_BRIDGE_TAG: ${OPENCLAW_BRIDGE_TAG:-unknown}
```
提交并推三 remote，随下次 cutover 生效。不做则尾注显示 `bridge=unknown`（不影响功能）。

- [ ] **Step 4: 真机验证（飞书表热控）**

1. 默认态：发一条 → 一张卡(⏳→答案) + 尾泡诊断行（`🔧 …`）。
2. 飞书「规则配置表」global-default 把 `调试尾注` 关掉 → POST `/admin/invalidate-feishu-group-policy`（或等 10min）→ 再发 → 尾泡变 `🌿 回复完毕`。
3. 把 `处理中卡片` 关掉 → 热失效 → 再发 → 不出占位卡（答案仍到达）。
4. 措辞：占位卡文案为新 ACK；连发第二条为新 REMIND。

- [ ] **Step 5: 回填结果到 spec/PR**

---

## Self-Review 记录

- **Spec 覆盖**：§4.1 rule 表读取+缓存+回退(Task 1)、invalidate(Task 2)、ensure 补字段(Task 3)；§4.2 尾注构造(Task 4)+build_reply 出口(Task 5)；§4.3 措辞(Task 6)；§4.4 infra tag(Task 7 Step 3，可选)；§9 测试计划逐条对应各 Task Step 1。均有任务。
- **类型一致**：`feishu_global_rule_policy()->dict[str,bool]`、`processing_card_enabled()/debug_trailer_enabled()->bool`、`build_feishu_trailer(details)->str`、`done_marker_text()->str`、`invalidate_global_rule_cache()->None` 跨任务一致。
- **占位符扫描**：无 TBD/TODO；每个代码步给了完整代码。
- **既有用例适配提示**：Task 1 Step 5 与 Task 5 Step 5 明确点出——`processing_card_enabled` 改走 policy、`build_reply` 成功出口从 `NO_REPLY` 变尾注——这两处既有占位卡用例可能需按新语义微调（属预期适配，非回归）。
- **范围**：代码全在 `openclaw_bridge.py` 单文件；compose 在 infra 仓且可选（已单列 Task 7 Step 3）。
