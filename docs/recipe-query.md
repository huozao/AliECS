# Recipe Query

The recipe query feature lets an authenticated user enter a formula code, parent item code, or parent name and generate a downloadable T+ BOM review workbook.

Troubleshooting, business-semantic pitfalls, and hot-patch steps live in `docs/runbooks/formula.md` (Chinese). This file documents the contract; that one documents the traps.

## Data Source

`backend-api` first reads the latest confirmed workbook from `RECIPE_ACTIVE_BOM_DIR`, default `/app/recipe-active-bom`. If no confirmed file exists, it falls back to the latest workbook from `RECIPE_BOM_INPUT_DIR`, default `/app/tplus-output/excel`. In local and production compose the fallback path is backed by the `tplus-sync-worker` output mounted read-only.

The source workbook may contain either:

- merged detail sheet `父件子件明细_提取`, or
- T+ export sheets `物料清单` and `子件明细`.

No real source workbook belongs in Git.

## Query Behavior

- `POST /v1/recipes/query` requires login and `formula.read`, or admin permissions.
- `default_bom=all` includes all BOM versions by default, including disabled rows if the source workbook contains them.
- `default_bom=1` filters to default BOM rows.
- `include_disabled=false` excludes rows whose `停用` value indicates disabled.
- Query output includes match count, recipe count, source workbook name, preview rows, a generated `file_id`, and a download URL.

## Reverse Lookup by Child Item

Added 2026-08-24. Finding "which formulas use this raw material" is a two-step flow, not a single query.

- `POST /v1/recipes/children/search` takes `{keyword, default_bom, include_disabled}` and lists candidate child items: `child_code`, `child_name`, `spec`, `unit`, `recipe_count` (distinct parent-code + version pairs using it). Sorted by `recipe_count` descending, capped at `CHILD_SEARCH_LIMIT` (200) with a `truncated` flag. Requires `formula.read`.
- `POST /v1/recipes/query` accepts optional `child_codes: list[str]` and `child_match: "any" | "all"`. When `child_codes` is non-empty the `query` field is display-only (audit trail and export filename) and does not participate in matching.
- Child codes are matched **exactly**, never as substrings, and the result contains **whole formulas** — every line of every matching version, not only the matching lines.
- `child_match="all"` requires a single formula version to contain every selected child. Interchangeable materials never co-occur, so `all` returns nothing for same-family selections; see the runbook.
- `RecipeCostRequest` extends `RecipeQueryRequest`, so `/v1/recipes/cost` and `/v1/recipes/cost/export` follow the same reverse-lookup scope automatically. The download context stores `child_codes` and `child_match` too, so deferred workbook generation stays consistent.

## Manual BOM Sync

- The homepage `手动同步配方` button calls `POST /v1/recipes/sync-bom`.
- This endpoint only creates a T+ BOM sync request file under `TPLUS_BOM_SYNC_REQUEST_DIR`; it does not run a broad full-system sync from the web request.
- `tplus-sync-worker` polls the same request directory and runs `job_sync_bom` when a request is present.
- `job_sync_bom` uses the BOM sync defaults, which query both enabled and disabled BOM rows.

## Reconciliation Active BOM

- `/health/` shows BOM snapshot differences with added, removed, and changed child rows.
- When an admin confirms a diff with the current or previous snapshot, `backend-api` creates a new active workbook named like `bom_20260606_061353.xlsx`.
- The confirmation response includes the generated filename, and later recipe queries prefer that active workbook.
- Historical diffs that are older than an accepted diff are marked `superseded`, so they no longer remain in the pending review list.

## Downloaded Workbook

`GET /v1/recipes/download/{file_id}` returns the generated workbook. The workbook includes:

- `配方表_人眼版`
- `横向对比_矩阵`
- `父件子件明细_提取`
- `版本父件分组合计`

Generated files are temporary runtime outputs under `RECIPE_EXPORT_DIR`.

## Security Boundary

- The API never returns local source paths, only the source file name.
- Download IDs must be service-generated 32-character hex IDs.
- The backend reads T+ output read-only and does not call Chanjet APIs from this route.
- Manual BOM sync requests contain no Chanjet tokens or secrets.
- Do not commit generated workbooks, raw BOM data, real tokens, or local env files.

## Validation

```powershell
python -m unittest AliECS.tests.test_recipe_query AliECS.tests.test_backend_recipes
docker compose -f AliECS\local\docker-compose.local.yml config
```

Frontend contracts are asserted as string checks in `tests/test_formula_frontend.py`; pure functions in `compare-core.js` / `cost-core.js` run under node via `tests/test_formula_core_js.py`.
