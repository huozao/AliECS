# Feishu Native Media and Bitable Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Feishu receive native images/PDF files and make the specified Bitable Base the authoritative, linked session console.

**Architecture:** WebDock serves generated artifacts with RFC-compliant filenames. The bridge consumes both `MEDIA:` and `FILE:` markers, uploads native Feishu media, and returns visible text. The bridge writes session/message/task data to the user-specified Base; a one-off migration establishes record-link fields and backfills user/group/session/rule records.

**Tech Stack:** Python, FastAPI/Starlette, Feishu Open API, pytest, Docker hot patches.

---

### Task 1: WebDock non-ASCII filenames

**Files:**
- Modify: `../webdock/src/api/routes_media.py`
- Test: `../webdock/tests/test_media_file_serving.py`

- [ ] Add a failing test proving a Chinese filename returns HTTP 200 with an ASCII-safe `filename` and UTF-8 `filename*`.
- [ ] Run the focused test and confirm the current `UnicodeEncodeError` behavior fails it.
- [ ] Encode `Content-Disposition` per RFC 5987 without changing stored bytes/MIME.
- [ ] Run focused and full WebDock tests.

### Task 2: Bridge native image/file delivery

**Files:**
- Modify: `deploy/openclaw-bridge/openclaw_bridge.py`
- Test: `tests/test_openclaw_bridge.py`

- [ ] Add failing tests for `MEDIA:` image upload/reply and visible failure fallback.
- [ ] Add failing tests for FILE failure returning visible text/link instead of legacy `MEDIA:`.
- [ ] Implement Feishu image multipart upload, native reply, unified marker delivery, and redacted success/failure traces.
- [ ] Run focused and full bridge tests.

### Task 3: Authoritative Bitable and record links

**Runtime target:** `SBbebjGw1ad7Xusj5LTcA1USnyI`

- [ ] Snapshot current/target table schemas and records.
- [ ] Point bridge runtime at target session/message/task table IDs.
- [ ] Create or convert record-link fields for user/group/session/message/task/rule relationships without deleting non-empty historical fields.
- [ ] Backfill current user, groups, active sessions, ChatGPT project/conversation URLs, and a default rule.
- [ ] Remove the unused default `数据表` only if it contains no non-empty cells; otherwise retain it and report.
- [ ] Verify target records and link field values via Feishu API.

### Task 4: Hot update and live verification

- [ ] Hot patch WebDock on `webdock`; preserve the old container file and restart only its API process/container as required.
- [ ] Hot patch `openclaw-bridge`; preserve the old container file and restart the bridge.
- [ ] Reuse the still-valid media tokens to verify native image and Chinese-named PDF delivery to the originating Feishu messages.
- [ ] Verify `/healthz`, `/v1/models`, container state, logs, Bitable URLs, and no new delivery errors.
- [ ] Stop after hot verification; do not perform formal image release/deployment until user confirms.
