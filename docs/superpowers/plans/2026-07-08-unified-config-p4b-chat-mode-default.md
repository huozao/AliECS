# Unified Config P4b Chat Mode Default Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenClaw bridge fall back to the system-config `对话模式默认` value when a Feishu conversation has no sticky chat mode.

**Architecture:** Sticky per-user/per-group mode remains stored on the existing session-console user/group tables. A new best-effort reader loads the singleton row in the system-config `对话模式` table using `FEISHU_SYSTEM_CONFIG_APP_TOKEN` and `FEISHU_SYSTEM_CONFIG_CHAT_MODE_TABLE_ID`; invalid/missing/unreadable values fall back to `advanced`. Record helpers gain an optional `app_token` parameter so the bridge can read the new independent Bitable app without duplicating pagination logic.

**Tech Stack:** Python bridge single-file service, Feishu Bitable helper functions, pytest.

---

## File Map

- Modify `deploy/openclaw-bridge/openclaw_bridge.py`
  - Parameterize Bitable record listing/search helpers with optional `app_token`.
  - Add chat-mode default constants and reader.
  - Make `feishu_chat_mode()` return the default only when no sticky mode is set.
- Modify `tests/test_openclaw_bridge.py`
  - Add helper parameterization tests.
  - Add default-mode tests for valid, missing, invalid, and sticky override behavior.
- Create `docs/superpowers/plans/2026-07-08-unified-config-p4b-chat-mode-default.md`
  - This implementation plan.

## Task 1: Failing Tests

**Files:**
- Modify: `tests/test_openclaw_bridge.py`

- [ ] **Step 1: Add record-helper app_token test**

Add near the system config helper tests:

```python
def test_find_feishu_bitable_record_uses_custom_app_token(monkeypatch):
    bridge = load_bridge()
    calls = []

    def fake_records(table_id, app_token=None):
        calls.append((table_id, app_token))
        return [{"fields": {"配置编号": "global-default", "对话模式默认": "均衡"}}]

    monkeypatch.setattr(bridge, "list_feishu_bitable_records", fake_records)
    record = bridge.find_feishu_bitable_record("tblMode", "配置编号", "global-default", app_token="sysApp")

    assert record["fields"]["对话模式默认"] == "均衡"
    assert calls == [("tblMode", "sysApp")]
```

- [ ] **Step 2: Add chat default tests**

Add after existing chat-mode tests:

```python
def test_feishu_chat_mode_default_reads_system_config(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SYSTEM_CONFIG_APP_TOKEN", "sysApp")
    monkeypatch.setenv("FEISHU_SYSTEM_CONFIG_CHAT_MODE_TABLE_ID", "tblMode")
    monkeypatch.delenv("FEISHU_SESSION_CONSOLE_USER_TABLE_ID", raising=False)
    monkeypatch.setattr(
        bridge,
        "find_feishu_bitable_record",
        lambda table, field, key, app_token=None: {
            "fields": {"配置编号": key, "对话模式默认": "均衡"},
        },
    )

    assert bridge.feishu_chat_mode(_mode_details(key="ou_mode_default_1")) == "balanced"


def test_feishu_chat_mode_default_falls_back_to_advanced(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SYSTEM_CONFIG_APP_TOKEN", "sysApp")
    monkeypatch.setenv("FEISHU_SYSTEM_CONFIG_CHAT_MODE_TABLE_ID", "tblMode")
    monkeypatch.delenv("FEISHU_SESSION_CONSOLE_USER_TABLE_ID", raising=False)
    monkeypatch.setattr(
        bridge,
        "find_feishu_bitable_record",
        lambda table, field, key, app_token=None: {
            "fields": {"配置编号": key, "对话模式默认": "乱写"},
        },
    )

    assert bridge.feishu_chat_mode(_mode_details(key="ou_mode_default_2")) == "advanced"


def test_feishu_chat_mode_sticky_overrides_system_default(monkeypatch):
    bridge = load_bridge()
    monkeypatch.setenv("FEISHU_SESSION_CONSOLE_USER_TABLE_ID", "tbl_user")
    monkeypatch.setenv("FEISHU_SYSTEM_CONFIG_APP_TOKEN", "sysApp")
    monkeypatch.setenv("FEISHU_SYSTEM_CONFIG_CHAT_MODE_TABLE_ID", "tblMode")

    def fake_find(table, field, key, app_token=None):
        if table == "tbl_user":
            return {"fields": {"open_id": key, "对话模式": "极速"}}
        return {"fields": {"配置编号": key, "对话模式默认": "高级"}}

    monkeypatch.setattr(bridge, "find_feishu_bitable_record", fake_find)

    assert bridge.feishu_chat_mode(_mode_details(key="ou_mode_default_3")) == "fast"
```

- [ ] **Step 3: Verify RED**

Run:

```powershell
python -m pytest tests/test_openclaw_bridge.py::test_find_feishu_bitable_record_uses_custom_app_token tests/test_openclaw_bridge.py::test_feishu_chat_mode_default_reads_system_config tests/test_openclaw_bridge.py::test_feishu_chat_mode_default_falls_back_to_advanced tests/test_openclaw_bridge.py::test_feishu_chat_mode_sticky_overrides_system_default -q
```

Expected: failures because helper signatures and default fallback do not exist yet.

## Task 2: Bridge Implementation

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`

- [ ] **Step 1: Parameterize record helpers**

Change signatures:

```python
def list_feishu_bitable_records(table_id: str, app_token: str | None = None) -> list[dict[str, Any]]:
    app_token = app_token or feishu_session_console_app_token()
    ...

def find_feishu_bitable_record(
    table_id: str,
    field_name: str,
    expected: str,
    app_token: str | None = None,
) -> dict[str, Any] | None:
    ...
    for record in list_feishu_bitable_records(table_id, app_token=app_token):
```

- [ ] **Step 2: Add default reader constants/functions**

Add near chat mode constants:

```python
CHATGPT_MODE_DEFAULT_FIELD = "对话模式默认"
CHATGPT_MODE_DEFAULT_RECORD_ID_FIELD = "配置编号"
CHATGPT_MODE_DEFAULT_RECORD_ID = "global-default"
CHATGPT_MODE_DEFAULT_FALLBACK = "advanced"
```

Add:

```python
def feishu_chat_mode_default() -> str:
    app_token = system_config_app_token()
    table_id = system_config_table_id("chat_mode")
    if not app_token or not table_id:
        return CHATGPT_MODE_DEFAULT_FALLBACK
    try:
        record = find_feishu_bitable_record(
            table_id,
            CHATGPT_MODE_DEFAULT_RECORD_ID_FIELD,
            CHATGPT_MODE_DEFAULT_RECORD_ID,
            app_token=app_token,
        )
        label = bitable_field_text(((record or {}).get("fields") or {}).get(CHATGPT_MODE_DEFAULT_FIELD)).strip()
        return CHATGPT_MODE_LABELS.get(label) or CHATGPT_MODE_DEFAULT_FALLBACK
    except Exception as exc:
        log_line(
            "feishu_chat_mode_default_read_failed "
            + json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True)
        )
        return CHATGPT_MODE_DEFAULT_FALLBACK
```

- [ ] **Step 3: Use default only when sticky mode is empty**

At the end of `feishu_chat_mode`, before caching/returning:

```python
    if not mode:
        mode = feishu_chat_mode_default()
```

- [ ] **Step 4: Run targeted tests**

Run:

```powershell
python -m pytest tests/test_openclaw_bridge.py::test_find_feishu_bitable_record_uses_custom_app_token tests/test_openclaw_bridge.py::test_feishu_chat_mode_default_reads_system_config tests/test_openclaw_bridge.py::test_feishu_chat_mode_default_falls_back_to_advanced tests/test_openclaw_bridge.py::test_feishu_chat_mode_sticky_overrides_system_default -q
```

Expected: pass.

## Task 3: Verification and PR

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`
- Modify: `tests/test_openclaw_bridge.py`
- Create: `docs/superpowers/plans/2026-07-08-unified-config-p4b-chat-mode-default.md`

- [ ] **Step 1: Run bridge tests**

Run:

```powershell
python -m pytest tests/test_openclaw_bridge.py -q
```

Expected: this suite may have known pre-existing failures; report exact failures and confirm the new targeted tests pass.

- [ ] **Step 2: Commit and PR with explicit paths**

Run:

```powershell
git status --short --branch
git add deploy/openclaw-bridge/openclaw_bridge.py tests/test_openclaw_bridge.py docs/superpowers/plans/2026-07-08-unified-config-p4b-chat-mode-default.md
git commit -m "feat(bridge): use system chat mode default"
git push -u origin feat/unified-config-p4b-chat-mode-default
gh pr create --repo huozao/AliECS --base main --head feat/unified-config-p4b-chat-mode-default --title "feat(bridge): use system chat mode default" --body "..."
```

- [ ] **Step 3: Merge/deploy without bridge cutover**

After checks pass, squash merge. Let release-deploy run, but do not run `bridge-cutover.yml`; bridge runtime adoption remains deferred until the planned cutover after the connection work.

## Self-Review

- Spec coverage: implements ④b only. ④a was completed in webdock separately. ③ backend config migration and ⑤ management page remain separate.
- Fallback behavior: missing/invalid/unreadable default returns `advanced`, matching the prior user-facing “默认（高级）” behavior.
- Scope: no SOPS changes, no bridge cutover, no production config mutation.
