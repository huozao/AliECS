# Unified Config P5 Effective Config Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an admin-facing effective configuration overview page that shows current values, sources, sync freshness, and editor links for the unified user configuration domains.

**Architecture:** Backend exposes one admin-only aggregation endpoint under `/v1/admin/system-config/effective`. The endpoint is read-only except that the UI reuses the existing `/v1/ops/doc-sync/sync-config` endpoint for the already-supported doc-sync emergency pause/resume. The first version does not add a global override table; unsupported emergency controls are shown as unavailable rather than silently invented.

**Tech Stack:** FastAPI, psycopg, existing backend helper functions, static `services/admin-ui/index.html`, Python pytest/unittest.

---

### Task 1: Backend Aggregation Endpoint

**Files:**
- Create: `services/backend-api/app/routers/system_config.py`
- Modify: `services/backend-api/app/main.py`
- Test: `tests/test_backend_system_config.py`

- [ ] **Step 1: Write failing backend tests**

Create `tests/test_backend_system_config.py` with tests for:

```python
def test_effective_config_returns_fallback_ready_shape_without_database():
    from app.routers.system_config import effective_system_config
    result = effective_system_config(_={})
    assert result["items"]
    assert any(item["domain"] == "doc_sync" for item in result["items"])
    assert any(row["key"] == "doc_sync.schedule" for row in result["items"][0]["rows"] or [])
```

Also add a test that monkeypatches helper functions so T+ descriptions and inventory ranges show source `系统配置镜像`, and doc-sync exposes `emergency.pause_supported=True`.

- [ ] **Step 2: Run backend tests and confirm RED**

Run:

```bash
python -m pytest tests/test_backend_system_config.py -q
```

Expected: fail with `ModuleNotFoundError` or missing route/function.

- [ ] **Step 3: Implement backend endpoint**

Create `services/backend-api/app/routers/system_config.py`:

```python
from __future__ import annotations

import os
from contextlib import closing
from typing import Any

from fastapi import APIRouter, Depends

from app.core import _conn, require_admin
from app.routers import exports
from app.routers.ops import _doc_sync_config_response, _read_doc_sync_config_row

router = APIRouter()

def _feishu_table_url(table_env_name: str) -> str | None:
    app_token = os.getenv("FEISHU_SYSTEM_CONFIG_APP_TOKEN", "").strip()
    table_id = os.getenv(table_env_name, "").strip()
    if not app_token or not table_id:
        return None
    return f"https://cloud.feishu.cn/base/{app_token}?table={table_id}"
```

Add helpers for `last_synced_at`, safe value formatting, and domain rows:
- `doc_sync`: read `_read_doc_sync_config_row()` and `_doc_sync_config_response()`.
- `chat_mode`: read `exports._system_config_record("对话模式")`, show mirrored value and note bridge direct-reads Feishu.
- `tplus_export`: read `exports._system_config_record("T+导出说明")`, show configured module count.
- `inventory_warehouse`: use `exports._inventory_scope_config()`.
- `features`: query `features` count when DB is available, otherwise fall back to `len(DEFAULT_FEATURES)`.

Endpoint:

```python
@router.get("/v1/admin/system-config/effective")
def effective_system_config(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return {"items": [_doc_sync_domain(), _chat_mode_domain(), _tplus_export_domain(), _inventory_domain(), _features_domain()]}
```

Each domain item must include:

```python
{
  "domain": "doc_sync",
  "title": "文档同步",
  "editor": {"label": "飞书系统配置 / 同步配置", "url": "... or None"},
  "last_synced_at": "... or None",
  "source": "系统配置镜像" | "默认/回退" | "Admin DB",
  "status": "ok" | "warn",
  "rows": [{"key": "...", "label": "...", "value": "...", "source": "..."}],
  "emergency": {"pause_supported": True, "pull_paused": False}
}
```

Modify `services/backend-api/app/main.py` to import and include `system_config_router`.

- [ ] **Step 4: Run backend tests and confirm GREEN**

Run:

```bash
python -m pytest tests/test_backend_system_config.py -q
```

Expected: all tests pass.

---

### Task 2: Admin UI Overview Section

**Files:**
- Modify: `services/admin-ui/index.html`
- Test: `tests/test_admin_frontend.py`

- [ ] **Step 1: Write failing frontend tests**

Extend `tests/test_admin_frontend.py` with assertions:

```python
def test_system_config_overview_is_rendered(self):
    self.assertIn('id="systemConfigPanel"', self.html)
    self.assertIn('api("/v1/admin/system-config/effective")', self.html)
    self.assertIn('renderSystemConfig()', self.html)
    self.assertIn('toggleDocSyncPullPaused', self.html)
```

- [ ] **Step 2: Run frontend test and confirm RED**

Run:

```bash
python -m pytest tests/test_admin_frontend.py -q
```

Expected: fail because the section and functions do not exist.

- [ ] **Step 3: Add overview UI**

In `services/admin-ui/index.html`:
- Add a card section before `功能入口管理`:

```html
<section id="systemConfigPanel" class="card">
  <h2>系统配置生效总览</h2>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>域</th>
          <th>配置项</th>
          <th>生效值</th>
          <th>来源</th>
          <th>最后同步</th>
          <th>编辑家</th>
          <th>应急</th>
        </tr>
      </thead>
      <tbody id="systemConfigBody"></tbody>
    </table>
  </div>
</section>
```

- Add `systemConfig: []` to state.
- Fetch `api("/v1/admin/system-config/effective")` in `loadAll()`.
- Add `renderSystemConfig()` to render rows.
- Add `toggleDocSyncPullPaused(paused)` that calls existing `/v1/ops/doc-sync/sync-config` with the latest values from the doc-sync domain emergency payload.

- [ ] **Step 4: Run frontend test and confirm GREEN**

Run:

```bash
python -m pytest tests/test_admin_frontend.py -q
```

Expected: all admin frontend tests pass.

---

### Task 3: Integration Verification and Review

**Files:**
- Review: `services/backend-api/app/routers/system_config.py`
- Review: `services/admin-ui/index.html`
- Review: `tests/test_backend_system_config.py`
- Review: `tests/test_admin_frontend.py`

- [ ] **Step 1: Run focused test set**

Run:

```bash
python -m pytest tests/test_backend_system_config.py tests/test_admin_frontend.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run adjacent backend/frontend tests**

Run:

```bash
python -m pytest tests/test_backend_ops_status.py tests/test_backend_exports.py tests/test_admin_frontend.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Review for scope boundaries**

Confirm:
- No production dependencies added.
- No secret values printed or committed.
- No new full-domain emergency override table added.
- Only doc-sync pause/resume uses existing write endpoint.
- Feature entry editor remains admin UI, not Feishu.

- [ ] **Step 4: Commit explicit file paths**

Run:

```bash
git add services/backend-api/app/routers/system_config.py services/backend-api/app/main.py services/admin-ui/index.html tests/test_backend_system_config.py tests/test_admin_frontend.py docs/superpowers/plans/2026-07-08-unified-config-p5-effective-config-page.md
git commit -m "feat(admin): add system config overview"
```

---

### Task 4: PR, Deploy, and Runtime Verification

**Files:**
- No additional file edits expected.

- [ ] **Step 1: Push branch and open PR**

Run:

```bash
git status --short --branch
git remote -v
git push -u origin feat/unified-config-p5-effective-page
gh pr create --base main --head feat/unified-config-p5-effective-page --title "feat(admin): add system config overview" --body "..."
```

- [ ] **Step 2: Watch PR checks**

Run:

```bash
gh pr checks --watch
```

Expected: checks pass.

- [ ] **Step 3: Merge and deploy**

Run:

```bash
gh pr merge --squash --delete-branch
gh run watch <release-deploy-run-id> --exit-status
```

Expected: release-deploy succeeds.

- [ ] **Step 4: Verify ECS runtime**

Run:

```bash
ssh aliecs 'cd /root/AliECS && git rev-parse --short HEAD && deploy/ecs/healthcheck.sh && deploy/ecs/post-deploy-smoke.sh'
```

Expected: HEAD matches merged main, healthcheck and smoke pass.

Optionally verify the endpoint from inside backend or via an admin token if available; do not print tokens or secrets.
