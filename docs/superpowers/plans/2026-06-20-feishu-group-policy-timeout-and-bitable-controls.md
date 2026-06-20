# Feishu Group Policy, Dynamic Timeout, and Bitable Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean Feishu group prompts, make group reply policy table-driven, extend active ChatGPT work safely to 20 minutes, and convert stable Bitable control fields to single-select in place.

**Architecture:** Keep Feishu prompt/policy behavior in the existing Bridge boundary, and keep response progress detection in WebDock's detector. Use an idempotent one-off migration for schema conversion and preserve all record-link fields.

**Tech Stack:** Python 3, pytest, Feishu OpenAPI, Docker hot-copy, SSH aliases `aliecs` and `webdock`.

---

### Task 1: Feishu prompt cleaning and group policy

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`
- Test: `tests/test_openclaw_bridge.py`

- [ ] Add failing tests proving the exact helper block and leading bot mention are removed while actual text/images remain.
- [ ] Add failing tests proving missing/blank group policy defaults to `回复所有`, `仅@回复` gates on a mention, disabled groups do not reply, and existing group settings are not overwritten.
- [ ] Run the focused tests and confirm failures are caused by current behavior.
- [ ] Implement Feishu-only cleaning, cached group-policy lookup, default-all behavior, and non-destructive group upserts.
- [ ] Re-run focused tests until green.

### Task 2: In-place Bitable control-field migration

**Files:**
- Modify: `deploy/openclaw-bridge/migrate_feishu_bitable_links.py`
- Test: `tests/test_openclaw_bridge.py`

- [ ] Add failing tests for exact single-select specifications, invalid-value preflight rejection, and relation-field preservation.
- [ ] Run focused migration tests and confirm they fail before implementation.
- [ ] Add idempotent `--convert-control-fields-to-select` handling that validates record values and updates existing field IDs with type 3/options.
- [ ] Re-run focused migration tests until green.

### Task 3: Progress-aware WebDock timeout

**Files:**
- Modify: `src/config.py`
- Modify: `src/browser/chatgpt_page.py`
- Modify: `src/browser/detector.py`
- Test: `tests/test_detector_response_wait.py`
- Test: `tests/test_runtime_overrides.py`

- [ ] Add failing tests proving post-soft-deadline progress renews the idle window, inactivity times out, and the 20-minute hard deadline wins even with continued progress.
- [ ] Add failing configuration tests for 15-second idle and 1200-second hard defaults/runtime overrides.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement progress fingerprints and pass the new settings through `ChatGPTPage.ask`.
- [ ] Re-run focused timeout/configuration tests until green.

### Task 4: Regression verification

**Files:**
- Verify only; no new files.

- [ ] Run `python -m pytest tests/test_openclaw_bridge.py -v` in AliECS.
- [ ] Run `python -m pytest -v` in AliECS.
- [ ] Run focused detector/config/media tests in WebDock.
- [ ] Run `python -m pytest -v` in WebDock.
- [ ] Review both worktree diffs and confirm no unrelated files changed.

### Task 5: Hot update and live verification

**Files/runtime:**
- ECS container: `/app/openclaw_bridge.py`
- ECS runtime env: `/opt/openclaw-bridge/webdock.env`
- WebDock container: `/app/src/config.py`, `/app/src/browser/chatgpt_page.py`, `/app/src/browser/detector.py`
- WebDock runtime env: deployed `.env` containing timeout variables
- Feishu Base: `飞书 ChatGPT 会话管理台`

- [ ] Back up live files, hot-copy verified source, set WebDock idle/hard and Bridge 1260-second timeouts, and restart only affected processes.
- [ ] Run the Bitable migration with `--convert-control-fields-to-select`.
- [ ] Verify Bridge/WebDock health and deployed file hashes.
- [ ] Read back all converted field types/options, existing record values, relation-field types/table IDs, and `hao` full-access permission.
- [ ] Report the Base URL/name, changed files, verification evidence, and hot-update-only rollback risk; do not formally deploy.
