# Unified Config P2 Doc Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move doc_sync schedule editing from legacy key/value rows to the new typed-column `同步配置` domain table while preserving safe rollout compatibility.

**Architecture:** `doc-sync-worker` continues to write the effective schedule into `integration_sync_config`. The puller now prefers the new singleton typed table `同步配置` (`配置编号=global-default`) and falls back to the old `配置表` key/value shape during migration. The new system config Bitable app is registered as a Feishu export/source for backup and observation, but that mirror is not part of the realtime effective path.

**Tech Stack:** Python `doc-sync-worker`, Feishu Bitable API client, Postgres-backed source registry, pytest.

---

## File Map

- Modify `services/doc-sync-worker/app/pipelines/sync_schedule.py`
  - Add typed table constants.
  - Parse singleton typed rows.
  - Prefer `同步配置` sources before legacy `配置表`.
  - Keep legacy key/value parsing intact.
- Modify `tests/test_doc_sync_worker.py`
  - Add typed-row parser tests.
  - Update puller tests to prove typed source preference and legacy fallback.
- Create `docs/superpowers/plans/2026-07-08-unified-config-p2-doc-sync.md`
  - This implementation plan.

## Task 1: Typed Parser Tests

**Files:**
- Modify: `tests/test_doc_sync_worker.py`

- [ ] **Step 1: Add typed parser tests**

Insert these tests in `SyncScheduleTests`, before the legacy `test_parse_config_rows_accepts_valid_keys_and_rejects_invalid_values` test:

```python
    def test_parse_config_rows_accepts_typed_singleton_row(self) -> None:
        from app.pipelines.sync_schedule import parse_config_rows

        config, errors = parse_config_rows(
            [
                {
                    "配置编号": "global-default",
                    "文档同步开关": True,
                    "文档同步周期小时": "6",
                    "文档同步起点时间": "02:00",
                }
            ]
        )

        self.assertEqual({"enabled": True, "interval_seconds": 21600, "anchor_time": "02:00"}, config)
        self.assertEqual([], errors)

    def test_parse_config_rows_typed_row_rejects_bad_values(self) -> None:
        from app.pipelines.sync_schedule import parse_config_rows

        config, errors = parse_config_rows(
            [
                {
                    "配置编号": "global-default",
                    "文档同步开关": True,
                    "文档同步周期小时": "0.5",
                    "文档同步起点时间": "25:00",
                },
                {
                    "配置编号": "draft",
                    "文档同步开关": False,
                    "文档同步周期小时": "24",
                    "文档同步起点时间": "",
                },
            ]
        )

        self.assertEqual({"enabled": True}, config)
        self.assertEqual(2, len(errors))
        self.assertIn("0.5", errors[0])
        self.assertIn("25:00", errors[1])
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```powershell
python -m pytest tests/test_doc_sync_worker.py::SyncScheduleTests::test_parse_config_rows_accepts_typed_singleton_row tests/test_doc_sync_worker.py::SyncScheduleTests::test_parse_config_rows_typed_row_rejects_bad_values -q
```

Expected: both tests fail because `parse_config_rows` ignores `配置编号` typed rows.

## Task 2: Typed Parser Implementation

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_schedule.py`

- [ ] **Step 1: Add typed constants**

Add near existing constants:

```python
DOC_SYNC_CONFIG_TABLE_SHEET_NAME = "同步配置"
LEGACY_CONFIG_TABLE_SHEET_NAME = "配置表"
DOC_SYNC_CONFIG_RECORD_ID_FIELD = "配置编号"
DOC_SYNC_CONFIG_RECORD_ID = "global-default"
```

Keep `CONFIG_TABLE_SHEET_NAME = LEGACY_CONFIG_TABLE_SHEET_NAME` if needed for compatibility with existing imports or tests.

- [ ] **Step 2: Add typed-row parser**

Add a helper before `parse_config_rows`:

```python
def _parse_typed_config_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    config: dict[str, Any] = {}
    errors: list[str] = []
    target = None
    for row in rows:
        record_id = _cell_text(row.get(DOC_SYNC_CONFIG_RECORD_ID_FIELD)).strip()
        if record_id == DOC_SYNC_CONFIG_RECORD_ID:
            target = row
            break
    if target is None:
        return config, errors
    for column, (field, parser) in CONFIG_REGISTRY.items():
        if column not in target:
            continue
        raw = _cell_text(target.get(column))
        try:
            config[field] = parser(raw)
        except (ValueError, TypeError) as exc:
            errors.append(f"{column}: {exc}")
    return config, errors
```

- [ ] **Step 3: Dispatch parser by shape**

Change `parse_config_rows` so typed rows are parsed first:

```python
def parse_config_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Parse doc_sync config rows.

    New shape: typed singleton row in ``同步配置`` with ``配置编号=global-default``.
    Legacy shape: ``配置表`` key/value rows, preserved during rollout.
    """
    if any(DOC_SYNC_CONFIG_RECORD_ID_FIELD in row for row in rows):
        return _parse_typed_config_rows(rows)
    ...
```

Leave the existing legacy body unchanged after this guard.

- [ ] **Step 4: Run parser tests**

Run:

```powershell
python -m pytest tests/test_doc_sync_worker.py::SyncScheduleTests::test_parse_config_rows_accepts_typed_singleton_row tests/test_doc_sync_worker.py::SyncScheduleTests::test_parse_config_rows_typed_row_rejects_bad_values tests/test_doc_sync_worker.py::SyncScheduleTests::test_parse_config_rows_accepts_valid_keys_and_rejects_invalid_values tests/test_doc_sync_worker.py::SyncScheduleTests::test_parse_config_rows_skips_disabled_rows_and_bad_anchor -q
```

Expected: all selected parser tests pass.

## Task 3: Puller Source Preference Tests

**Files:**
- Modify: `tests/test_doc_sync_worker.py`

- [ ] **Step 1: Update fake client to emit typed fields by table**

Replace the puller test fake client with a table-aware version:

```python
        class FakeClient:
            def list_fields(self, app_token: str, table_id: str) -> list[dict]:
                if table_id == "tbl_sync":
                    return [
                        {"field_id": "f_id", "field_title": "配置编号"},
                        {"field_id": "f_enabled", "field_title": "文档同步开关"},
                        {"field_id": "f_interval", "field_title": "文档同步周期小时"},
                        {"field_id": "f_anchor", "field_title": "文档同步起点时间"},
                    ]
                return [
                    {"field_id": "f_key", "field_title": "配置键"},
                    {"field_id": "f_val", "field_title": "配置值"},
                    {"field_id": "f_status", "field_title": "状态"},
                ]

            def get_records(self, app_token: str, table_id: str, view_id: str = "") -> dict:
                if table_id == "tbl_sync":
                    return {
                        "records": [
                            {
                                "record_id": "r_sync",
                                "fields": {
                                    "f_id": "global-default",
                                    "f_enabled": False,
                                    "f_interval": "12",
                                    "f_anchor": "03:30",
                                },
                            }
                        ],
                        "page_count": 1,
                    }
                return {
                    "records": [
                        {"record_id": "r1", "fields": {"f_key": "文档同步周期小时", "f_val": "6", "f_status": "启用"}},
                        {"record_id": "r2", "fields": {"f_key": "文档同步起点时间", "f_val": "02:00", "f_status": "启用"}},
                    ],
                    "page_count": 1,
                }
```

- [ ] **Step 2: Add typed source to fake store**

Change `FakeStore.list_bitable_sources` to return `同步配置` before other sources:

```python
                return [
                    {
                        "external_doc_id": "bascn_system_config",
                        "external_sheet_id": "tbl_sync",
                        "document_name": "系统配置",
                        "sheet_name": "同步配置",
                        "source_url": "",
                    },
                    {
                        "external_doc_id": "bascn_console",
                        "external_sheet_id": "tbl_cfg",
                        "document_name": "飞书 ChatGPT 会话管理台",
                        "sheet_name": "配置表",
                        "source_url": "",
                    },
                ]
```

Update assertions in the same test:

```python
        self.assertEqual(43200, store.saved["interval_seconds"])
        self.assertEqual("03:30", store.saved["anchor_time"])
        self.assertEqual(False, store.saved["enabled"])
        self.assertEqual("feishu-system-config-table", store.saved["updated_by"])
```

- [ ] **Step 3: Add legacy fallback test**

Add a second puller test that returns only the old `配置表` source and asserts the existing `6` hour / `02:00` behavior still applies with `updated_by="feishu-config-table"`.

- [ ] **Step 4: Run tests to verify RED**

Run:

```powershell
python -m pytest tests/test_doc_sync_worker.py::SyncScheduleTests::test_pull_config_writes_db_when_changed_and_respects_pause -q
```

Expected: fails until the puller prefers `同步配置`.

## Task 4: Puller Implementation

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_schedule.py`

- [ ] **Step 1: Add source selection helper**

Add before `pull_config_from_bitable`:

```python
def _select_config_source(sources: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    typed = [row for row in sources if str(row.get("sheet_name") or "") == DOC_SYNC_CONFIG_TABLE_SHEET_NAME]
    if typed:
        return typed[0], "feishu-system-config-table"
    legacy = [row for row in sources if str(row.get("sheet_name") or "") == LEGACY_CONFIG_TABLE_SHEET_NAME]
    if legacy:
        return legacy[0], "feishu-config-table"
    return None, ""
```

- [ ] **Step 2: Use selected source and updated status strings**

Inside `pull_config_from_bitable`, replace the current list comprehension filtered only to `CONFIG_TABLE_SHEET_NAME` with:

```python
            source, updated_by = _select_config_source(store.list_bitable_sources("feishu", profile))
            if not source:
                continue
```

Use `updated_by=updated_by` in `store.upsert_sync_config`.

Change log/status wording from `配置表` to `同步配置/配置表`:

```python
                print(f"[同步配置拉取] 非法值已跳过：{error}")
...
                return "noop: 同步配置/配置表无可用配置项"
...
        return "noop: 未找到「同步配置」或「配置表」数据源"
```

- [ ] **Step 3: Run targeted tests**

Run:

```powershell
python -m pytest tests/test_doc_sync_worker.py::SyncScheduleTests -q
```

Expected: all `SyncScheduleTests` pass.

## Task 5: Register System Config Bitable as Export Source

**Files:**
- No repository code changes.

- [ ] **Step 1: Confirm secret render on aliecs**

Run:

```powershell
ssh aliecs 'grep -E "^FEISHU_SYSTEM_CONFIG_APP_TOKEN=" /opt/openclaw-bridge/webdock.env >/dev/null && echo SET'
```

Expected: `SET`. Do not print token values.

- [ ] **Step 2: Upsert Feishu app source row**

Run a redacted server-side script that reads `FEISHU_SYSTEM_CONFIG_APP_TOKEN` from `/opt/openclaw-bridge/webdock.env`, reads Postgres credentials from `/root/AliECS/deploy/ecs/release-meta.env`, and upserts one active source row:

```sql
provider='feishu'
env_profile='COMPANY_A'
external_doc_id=<FEISHU_SYSTEM_CONFIG_APP_TOKEN>
external_sheet_id=''
source_type='bitable_app'
document_name='系统配置'
source_name='系统配置'
sheet_name=''
enabled=true
```

Expected: one row exists in `external_sources` and no secrets are printed.

- [ ] **Step 3: Trigger one Feishu sync for discovery**

Create or use an existing sync request for the system config app source so `sync_feishu_source()` rescans the app and persists table-level `bitable_table` rows for `同步配置`, `bridge规则`, `对话模式`, `T+导出说明`, and `库存仓库范围`.

Expected: `external_sources` contains the app row plus table rows for the five domain tables.

- [ ] **Step 4: Verify exports visibility**

Use the live catalog endpoint if authenticated admin access is available, otherwise query Postgres for active Feishu sources with `document_name='系统配置'`.

Expected: the system config Bitable is present in the Feishu export/catalog source set. This is only the backup/observation path and does not drive realtime effective config.

## Task 6: Final Verification, Commit, PR, Deploy

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/sync_schedule.py`
- Modify: `tests/test_doc_sync_worker.py`
- Create: `docs/superpowers/plans/2026-07-08-unified-config-p2-doc-sync.md`

- [ ] **Step 1: Run smallest relevant tests**

Run:

```powershell
python -m pytest tests/test_doc_sync_worker.py::SyncScheduleTests -q
```

Expected: pass.

- [ ] **Step 2: Run worker suite**

Run:

```powershell
python -m pytest tests/test_doc_sync_worker.py -q
```

Expected: pass.

- [ ] **Step 3: Commit with explicit paths only**

Run:

```powershell
git status --short --branch
git branch --show-current
git remote -v
git add services/doc-sync-worker/app/pipelines/sync_schedule.py tests/test_doc_sync_worker.py docs/superpowers/plans/2026-07-08-unified-config-p2-doc-sync.md
git commit -m "feat(doc-sync): support typed system config table"
```

Expected: commit contains only the three explicit files.

- [ ] **Step 4: Open AliECS PR**

Push branch and open a PR against `main`:

```powershell
git push -u origin feat/unified-config-p2-doc-sync
gh pr create --repo huozao/AliECS --base main --head feat/unified-config-p2-doc-sync --title "feat(doc-sync): support typed system config table" --body "..."
```

Expected: PR created with tests and rollout notes.

- [ ] **Step 5: Merge and deploy after checks pass**

After PR checks pass and review is complete, merge per repository convention. Push to `main` triggers `release-deploy.yml`.

Expected: release deploy succeeds. Do not run bridge cutover in this plan.

- [ ] **Step 6: Runtime verification**

On aliecs:

```powershell
ssh aliecs 'cd /root/AliECS && deploy/ecs/healthcheck.sh'
ssh aliecs 'cd /root/AliECS && deploy/ecs/post-deploy-smoke.sh'
```

Expected: healthcheck and smoke pass. Verify doc-sync config pull logs or DB state show `updated_by` from `feishu-system-config-table` after the new system config source is synced.

## Self-Review

- Spec coverage: covers D2 doc_sync typed-column migration and D1 backup/observation source registration. It does not implement backend T+ settings, warehouse option sync, bridge chat-mode default, or the final management page; those belong to plans ③, ④b, and ⑤.
- Rollout safety: legacy `配置表` support remains until production source registration and sync are confirmed.
- Secret safety: source registration reads token server-side and never prints token or table IDs.
