# Formula Cost Simulation RBAC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add simulated formula-cost quantities/ratios/amounts, Excel export per recipe, and separate RBAC for cost calculation.

**Architecture:** Keep formula math in `services/backend-api/app/recipes/bom_query.py` so UI and export use one ratio rule. Keep RBAC data in idempotent SQL migration and backend route guards; the admin UI continues to render roles and permissions from APIs.

**Tech Stack:** FastAPI, pandas/openpyxl, vanilla HTML/JS, PostgreSQL migrations, unittest.

---

### Task 1: Baseline And RED Tests

**Files:**
- Modify: `tests/test_recipe_query.py`
- Modify: `tests/test_backend_recipes.py`
- Modify: `tests/test_backend_ops_status.py`
- Create: `db/migrations/0012_formula_cost_rbac.sql`

- [x] Run baseline targeted recipe tests before behavior changes.
- [x] Add a unit test proving simulated quantities recompute simulated ratios with the same packaging exclusion rule as `compute_ratio_series`.
- [x] Add backend route tests proving `/v1/recipes/cost` now requires `formula.cost.calculate`, accepts simulated quantities, and `/v1/recipes/cost/export` returns an xlsx workbook with one sheet per recipe.
- [x] Add a default-feature/RBAC seed test for `formula.cost.calculate` and the 11 requested role codes.
- [x] Run the new tests and confirm they fail for missing behavior or missing seed data.

### Task 2: Backend Cost Calculation And Export

**Files:**
- Modify: `services/backend-api/app/recipes/bom_query.py`
- Modify: `services/backend-api/app/main.py`

- [x] Extend `calculate_recipe_costs` with `simulated_quantities: dict[str, float] | None`.
- [x] Build simulated ratios per `(version, parent_code)` group by replacing only supplied child quantities, converting grams to kg, and excluding existing skip child codes plus units `条` and `个`.
- [x] Return `simulated_quantity`, `simulated_ratio`, `simulated_amount`, and `simulated_total`; keep existing `quantity`, `ratio`, `system_amount`, and `current_amount` behavior stable.
- [x] Add `save_recipe_cost_workbook(output_path, recipes)` that creates one Excel sheet per recipe with safe unique sheet names.
- [x] Extend `RecipeCostRequest` with `simulated_quantities` and require `formula.cost.calculate` for cost and export routes.
- [x] Add `POST /v1/recipes/cost/export` that recomputes current cost data and returns an xlsx file.
- [x] Run backend unit tests and fix failures.

### Task 3: RBAC Seed And Documentation

**Files:**
- Create: `db/migrations/0012_formula_cost_rbac.sql`
- Modify: `services/backend-api/app/main.py`
- Modify: `docs/auth-rbac-guide.md`

- [x] Seed roles: `chairman`, `general_manager_a`, `general_manager_b`, `sales_a`, `sales_b`, `tech_a`, `tech_b`, `finance_a`, `finance_b`, `warehouse_a`, `warehouse_b`.
- [x] Seed permission `formula.cost.calculate` named `配方成本核算`.
- [x] Attach `formula.cost.calculate` to `admin` only by default; later role assignment remains manual in `/admin/`.
- [x] Update `DEFAULT_FEATURES` so `formula_query` remains visible via `formula.read`; do not hide the whole page behind cost permission.
- [x] Update RBAC guide with the new role list and separate cost permission.

### Task 4: Formula UI

**Files:**
- Modify: `services/public-web/formula/index.html`

- [x] Add `SIM_QTY_KEY` localStorage state for simulated quantities.
- [x] Add table headers `模拟数量`, `模拟比例`, and `模拟分价`.
- [x] Render simulated quantity inputs per child code and show simulated ratio/amount from backend response.
- [x] On simulated quantity change or Enter, validate non-negative number, persist it, refetch `/v1/recipes/cost`, and keep focus behavior ergonomic.
- [x] Add reset behavior for simulated quantities and keep existing price reset behavior stable.
- [x] Add `导出核算Excel` button calling `/v1/recipes/cost/export` with query scope, manual prices, and simulated quantities.

### Task 5: Verification

**Files:**
- No planned code edits unless verification exposes defects.

- [x] Run `python -m unittest discover -s tests -p "test_recipe_query.py" -v`.
- [x] Run `python -m unittest discover -s tests -p "test_backend_recipes.py" -v`.
- [x] Run `python -m unittest discover -s tests -p "test_backend_ops_status.py" -v`.
- [x] Run `python -m unittest discover -s tests -v`.
- [x] Run `git diff --check`.
- [x] Report changed files, verification output, and any remaining risks. Do not commit, push, or deploy without explicit approval.
