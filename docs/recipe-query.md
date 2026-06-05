# Recipe Query

The recipe query feature lets an authenticated user enter a formula code, parent item code, or parent name and generate a downloadable T+ BOM review workbook.

## Data Source

`backend-api` reads the latest workbook from `RECIPE_BOM_INPUT_DIR`, default `/app/tplus-output/excel`. In local and production compose this path is backed by the `tplus-sync-worker` output mounted read-only.

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
- Do not commit generated workbooks, raw BOM data, real tokens, or local env files.

## Validation

```powershell
python -m unittest AliECS.tests.test_recipe_query AliECS.tests.test_backend_recipes
docker compose -f AliECS\local\docker-compose.local.yml config
```
