# Feishu Session Config Home Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Move all Feishu ChatGPT session-related configuration back to the existing session console and add per-user/per-group project link overrides.

**Architecture:** The session console rule table owns Feishu ChatGPT global defaults. User/group tables own peer-level overrides. The independent system-config app remains for non-ChatGPT domains only.

**Tech Stack:** Python bridge, Feishu Bitable API helpers, FastAPI admin overview, pytest/unittest.

---

### Task 1: Bridge Rule Defaults

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`
- Test: `tests/test_openclaw_bridge.py`

- [x] Add tests proving `feishu_chat_mode_default()` reads `对话模式默认` from `FEISHU_SESSION_CONSOLE_RULE_TABLE_ID`.
- [x] Add tests proving `ensure_feishu_default_rule_record()` backfills `对话模式默认`, `默认新对话项目链接`, and `默认新对话项目名称`.
- [x] Implement rule-table default readers with env fallback and no dependency on `FEISHU_SYSTEM_CONFIG_CHAT_MODE_TABLE_ID`.
- [x] Run `python -m pytest tests/test_openclaw_bridge.py::<new-tests> -q`.

### Task 2: Project Link Overrides

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`
- Test: `tests/test_openclaw_bridge.py`

- [x] Add tests for project URL priority: existing session record, group override, user override, global rule, env.
- [x] Add helper to read peer project config from group/user table.
- [x] Use the helper in `enrich_feishu_metadata_with_session_route()` and `build_feishu_session_index_fields()`.
- [x] Ensure user/group upserts create/backfill project link columns.
- [x] Run focused bridge tests.

### Task 3: Names And Timestamps

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`
- Modify: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`
- Test: `tests/test_openclaw_bridge.py`

- [x] Add tests that existing human-readable `群名称` / `飞书用户名` are not overwritten by opaque IDs.
- [x] Add `最近名称解析时间` to user/group schemas.
- [x] Update user/group upsert logic to only overwrite names when current value is blank or machine-like.
- [x] Run focused bridge tests.

### Task 4: Admin Overview Boundary

**Files:**
- Modify: `services/backend-api/app/routers/system_config.py`
- Test: `tests/test_backend_system_config.py`

- [x] Update tests so effective system config includes doc-sync, T+ export, inventory, features, but not chat mode.
- [x] Remove `_chat_mode_domain()` from the overview response.
- [x] Run `python -m pytest tests/test_backend_system_config.py -q`.

### Task 5: Bitable Schema Bootstrap

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`
- Test: existing bootstrap tests if present

- [x] Add new fields to session console bootstrap schema: rule defaults, user/group project overrides, name resolution time.
- [x] Run `python -m pytest tests/test_doc_sync_worker.py -q` if feasible; otherwise run the relevant test class.

### Task 6: Integration And Release

**Files:**
- Modify only files from Tasks 1-5.

- [x] Run focused bridge/backend/doc-sync tests.
- [x] Check `git status` and confirm no secrets or unrelated files are staged.
- [x] Commit with explicit paths only.
- [x] Push branch and open PR.
- [x] After merge/deploy, do not cut over bridge; user will verify bridge cutover result.
