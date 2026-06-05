# Recipe Query Module

This folder contains pure workbook logic for AliECS recipe query.

## Inputs

- Runtime-only T+ BOM workbooks with `物料清单` and `子件明细` sheets.
- The source workbook can be passed directly to `query_recipe_workbook`.
- Future API wiring may locate workbooks from `RECIPE_BOM_INPUT_PATH` or `RECIPE_BOM_INPUT_DIR`.

## Outputs

- `query_recipe_workbook` returns matched recipe detail and grouped summary data.
- `save_recipe_workbook` writes a review workbook with:
  - `配方表_人眼版`
  - `横向对比_矩阵`
  - `父件子件明细_提取`
  - `版本父件分组合计`

## Security Boundary

- No FastAPI route, database client, `.env` loader, or real filesystem path is required by this pure module.
- No real BOM workbook, token, `.env`, or generated business output belongs in Git.
- Runtime inputs and exports should stay in ignored folders, Docker volumes, or private server paths.
- Files written under `RECIPE_EXPORT_DIR` are runtime temporary exports; deployment should clean them periodically, and generated files must not be committed to Git.

## Verification

Run from the workspace parent:

```powershell
python -m unittest AliECS.tests.test_recipe_query
```
