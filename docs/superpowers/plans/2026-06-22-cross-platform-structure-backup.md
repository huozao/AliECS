# Cross-Platform Structure Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorder the existing backup workbook, include the workbook itself in WeCom A, and add Feishu application/table structure backup in the same workbook.

**Architecture:** Keep Postgres as the normalized structure source and the existing durable job queue as the scheduler. Add platform-aware snapshots and a safe worksheet rebuild path because WeCom cannot move existing fields in place. Register synthetic document anchors for the backup workbook and Feishu apps so every row remains one document/application.

**Tech Stack:** Python 3.12, psycopg 3, WeCom Wedoc OpenAPI, Feishu Bitable OpenAPI, PostgreSQL, pytest, Docker Compose.

---

### Task 1: Lock field order and worksheet migration behavior

**Files:**
- Modify: `tests/test_wecom_structure_backup.py`
- Modify: `services/doc-sync-worker/app/pipelines/wecom_structure_backup.py`
- Modify: `services/doc-sync-worker/app/providers/wecom.py`

- [ ] Add failing tests asserting the exact common field prefix and four worksheet titles.
- [ ] Add a failing fake-client test: order mismatch creates a temporary sheet, copies records, verifies count, swaps names, and deletes the old sheet only after success.
- [ ] Run `python -m pytest -q tests/test_wecom_structure_backup.py` and confirm failures are caused by missing ordering/migration behavior.
- [ ] Add `update_sheet`, `update_fields`, and safe worksheet rebuild helpers; preserve raw cell element arrays during record copy.
- [ ] Run the focused test until green.

### Task 2: Register and snapshot the backup workbook itself

**Files:**
- Modify: `tests/test_wecom_structure_backup.py`
- Modify: `services/doc-sync-worker/app/storage/postgres.py`
- Modify: `services/doc-sync-worker/app/pipelines/wecom_structure_backup.py`

- [ ] Add failing tests for a `structure_backup_doc` anchor plus `structure_backup_sheet` children and field replacement without records.
- [ ] Add storage methods that upsert a generic document anchor and remove stale child sources.
- [ ] Add `refresh_backup_workbook_structure` using `get_sheets/get_fields`; remove the backup-doc exclusions from enqueue/consume.
- [ ] Verify focused tests pass and the backup source type remains excluded from normal `list_registry_doc_sources`.

### Task 3: Add Feishu app/table structure snapshots

**Files:**
- Modify: `tests/test_wecom_structure_backup.py`
- Modify: `tests/test_doc_sync_worker.py`
- Modify: `services/doc-sync-worker/app/storage/postgres.py`
- Modify: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`
- Modify: `services/doc-sync-worker/app/pipelines/wecom_structure_backup.py`

- [ ] Add failing tests grouping multiple `bitable_table` rows by app_token into one application snapshot.
- [ ] Add a generic `bitable_app` anchor during Feishu full sync and a `list_feishu_document_structures` store query.
- [ ] Extend `DocumentStructureSnapshot` with platform/target worksheet and build Feishu values with app_token/table_id.
- [ ] Extend daily enqueue and job consumption to dispatch by provider and write Feishu changes into `飞书-最新结构` plus common history.
- [ ] Run focused worker tests until green.

### Task 4: Update runtime docs/version and migration compatibility

**Files:**
- Modify: `docs/doc-sync-design.md`
- Modify: `CHANGELOG.md`
- Modify: `VERSION`
- Modify if needed: `db/migrations/0017_wecom_structure_backup.sql`

- [ ] Document four worksheet names, target field order, self-registration, Feishu grouping, and safe migration.
- [ ] Bump version to `v2.1.14` and add the changelog entry.
- [ ] Run `python scripts/validate_version.py`, Python compile, compose config, and `git diff --check`.

### Task 5: Full local verification

- [ ] Run `python -m pytest -q tests/test_wecom_structure_backup.py tests/test_doc_sync_worker.py tests/test_wecom_sheet_sync.py`.
- [ ] Run `python -m pytest -q tests`.
- [ ] Run `$env:PYTHONPATH='src'; python -m pytest -q tests` from `services/tplus-sync-worker`.
- [ ] Confirm no temporary files, secrets, runtime env, logs, browser data, or unrelated changes are included.

### Task 6: ECS hot deployment and live migration

- [ ] Stop the doc-sync worker before worksheet migration.
- [ ] Copy only changed worker/migration/compose files to `/root/AliECS`, build the local hot-update image, and run migrations.
- [ ] Run workbook bootstrap/ensure once; verify the safe migration preserved current A/B/history rows and created `飞书-最新结构`.
- [ ] Run Feishu full sync, enqueue/consume all structure tasks, and verify the backup workbook itself appears in WeCom A.
- [ ] Read back field order and row counts from all four worksheets; rerun selected jobs and confirm `changed=False` and no duplicate history.
- [ ] Restart the persistent worker and run ECS health checks.

No commit or push is performed because the user has not authorized Git writes or GitHub publication.
