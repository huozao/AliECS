# Unified Config P3 Backend DB Mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move backend T+ export descriptions and inventory warehouse scope from hardcoded-only values to the system-config Bitable mirror stored by doc-sync.

**Architecture:** Backend does not read Feishu directly in this phase. It reads the doc-sync mirror from `external_sources`/`external_records` for the `系统配置` app and falls back to code defaults if the mirror is missing, stale, invalid, or DB is unavailable. Inventory warehouse selections are validated against the latest T+ `warehouse_*.xlsx` archive before they affect current-stock filtering.

**Tech Stack:** FastAPI backend router helpers, Postgres mirror tables, T+ Excel exports, pytest/unittest.

---

## File Map

- Modify `services/backend-api/app/routers/exports.py`
  - Add system-config mirror read helpers.
  - Make `_tplus_export_description()` prefer the mirrored `T+导出说明` singleton row.
  - Make inventory scope filtering prefer mirrored `库存仓库范围` multi-select fields after warehouse-code validation.
- Modify `tests/test_backend_exports.py`
  - Add tests for mirrored T+ export descriptions and fallback.
- Modify `tests/test_backend_inventory_stock.py`
  - Add tests for mirrored warehouse scope and invalid-code fallback.
- Create `docs/superpowers/plans/2026-07-08-unified-config-p3-backend-db-mirror.md`
  - This plan.

## Task 1: Description Mirror Tests

**Files:**
- Modify: `tests/test_backend_exports.py`

- [ ] **Step 1: Add mirrored-description test**

Add a test that monkeypatches `self.main._system_config_record`:

```python
    def test_tplus_export_description_uses_system_config_mirror(self) -> None:
        old_record = self.main._system_config_record
        self.main._system_config_record = lambda sheet: {"bom": "配置里的 BOM 说明"} if sheet == "T+导出说明" else {}
        try:
            self.assertEqual("配置里的 BOM 说明", self.main._tplus_export_description("bom"))
        finally:
            self.main._system_config_record = old_record
```

- [ ] **Step 2: Add fallback test**

```python
    def test_tplus_export_description_falls_back_when_mirror_empty(self) -> None:
        old_record = self.main._system_config_record
        self.main._system_config_record = lambda sheet: {}
        try:
            self.assertIn("BOM 父件和子件", self.main._tplus_export_description("bom"))
            self.assertIn("暂未配置说明", self.main._tplus_export_description("unknown_module"))
        finally:
            self.main._system_config_record = old_record
```

- [ ] **Step 3: Verify RED**

Run:

```powershell
python -m pytest tests/test_backend_exports.py::BackendExportsTests::test_tplus_export_description_uses_system_config_mirror tests/test_backend_exports.py::BackendExportsTests::test_tplus_export_description_falls_back_when_mirror_empty -q
```

Expected: fails because `_system_config_record` does not exist and descriptions are hardcoded-only.

## Task 2: Inventory Scope Tests

**Files:**
- Modify: `tests/test_backend_inventory_stock.py`

- [ ] **Step 1: Add mirrored warehouse scope test**

Add a test that writes `current_stock` and `warehouse` exports, monkeypatches `_system_config_record`, then checks raw and finished scope:

```python
    def test_inventory_scope_uses_system_config_mirror(self) -> None:
        directory = Path(self._tmp.name)
        current = directory / "current_stock_20260610_120000.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append([
            "WarehouseCode", "WarehouseName", "InventoryCode", "InventoryName",
            "InventoryClassName", "Specification", "UnitName", "ExistingQuantity", "AvailableQuantity",
        ])
        ws.append(["001", "原材库", "A1", "ABS", "原材料", "", "kg", "10", "10"])
        ws.append(["002", "成品库", "B1", "成品", "物料清单", "", "kg", "20", "20"])
        ws.append(["007", "呆滞库", "C1", "呆滞", "物料清单", "", "kg", "5", "5"])
        wb.save(current)
        warehouse = directory / "warehouse_20260610_120000.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(["WarehouseCode", "WarehouseName"])
        ws.append(["001", "原材库"])
        ws.append(["002", "成品库"])
        ws.append(["007", "呆滞库"])
        wb.save(warehouse)
        old_record = self.main._system_config_record
        self.main._system_config_record = lambda sheet: {
            "库存原料仓库": ["002"],
            "成品排除仓库": ["007"],
        } if sheet == "库存仓库范围" else {}
        try:
            user = self._user(permissions=["inventory.raw.read", "inventory.finished.read"])
            raw = self.main.inventory_current_stock(q="", warehouse="", scope="raw", user=user)
            finished = self.main.inventory_current_stock(q="", warehouse="", scope="finished", user=user)
        finally:
            self.main._system_config_record = old_record
        self.assertEqual({"002"}, {item["WarehouseCode"] for item in raw["items"]})
        self.assertEqual({"001", "002"}, {item["WarehouseCode"] for item in finished["items"]})
```

- [ ] **Step 2: Add invalid-code fallback test**

Add a test where config selects only `999`, warehouse archive has no `999`, and raw scope falls back to hardcoded `001`/`012`.

- [ ] **Step 3: Verify RED**

Run:

```powershell
python -m pytest tests/test_backend_inventory_stock.py::InventoryCurrentStockTests::test_inventory_scope_uses_system_config_mirror -q
```

Expected: fails because inventory scope still uses hardcoded sets.

## Task 3: Backend Implementation

**Files:**
- Modify: `services/backend-api/app/routers/exports.py`

- [ ] **Step 1: Add system-config mirror helpers**

Add helpers that query the active Feishu source where `document_name='系统配置'` and `sheet_name=<sheet>`, returning the `raw_json.fields` from the singleton row with `配置编号='global-default'`. Catch all exceptions and return `{}`.

- [ ] **Step 2: Add cell parsers**

Add `_config_text(value)` and `_config_codes(value)` so raw JSON values can be strings or lists. Lists are trimmed and de-duplicated in order.

- [ ] **Step 3: Use mirrored descriptions**

Change `_tplus_export_description(module)` to return non-empty mirrored text from `T+导出说明` first, then `_TPLUS_EXPORT_DESCRIPTIONS`, then the existing unknown fallback.

- [ ] **Step 4: Validate inventory warehouse scope**

Add `_latest_warehouse_archive_codes()` to read latest `warehouse_*.xlsx`; support `WarehouseCode`, `仓库编码`, and `Code` columns. Add `_inventory_scope_config()` that validates mirrored `库存原料仓库` and `成品排除仓库` against archive codes when available, and falls back to defaults if the resulting set is empty.

- [ ] **Step 5: Wire current-stock filtering**

In `inventory_current_stock`, replace direct `_RAW_STOCK_WAREHOUSES` / `_FINISHED_EXCLUDED_WAREHOUSES` use with `_inventory_scope_config()`.

- [ ] **Step 6: Run targeted tests**

Run:

```powershell
python -m pytest tests/test_backend_exports.py tests/test_backend_inventory_stock.py -q
```

Expected: pass.

## Task 4: PR, Deploy, Runtime Verification

**Files:**
- Modify: `services/backend-api/app/routers/exports.py`
- Modify: `tests/test_backend_exports.py`
- Modify: `tests/test_backend_inventory_stock.py`
- Create: `docs/superpowers/plans/2026-07-08-unified-config-p3-backend-db-mirror.md`

- [ ] **Step 1: Commit with explicit paths only**

```powershell
git status --short --branch
git add services/backend-api/app/routers/exports.py tests/test_backend_exports.py tests/test_backend_inventory_stock.py docs/superpowers/plans/2026-07-08-unified-config-p3-backend-db-mirror.md
git commit -m "feat(backend): read T+ config from system mirror"
```

- [ ] **Step 2: Open PR and merge after checks pass**

Create a PR against `main`; include verification and note that backend uses DB mirror, not realtime Feishu direct-read.

- [ ] **Step 3: Deploy and verify**

After merge, wait for `release-deploy.yml`. Then run:

```powershell
ssh aliecs 'cd /root/AliECS && deploy/ecs/healthcheck.sh && deploy/ecs/post-deploy-smoke.sh'
```

Verify the system-config Feishu source still has app + five active domain tables and that `/v1/exports/catalog` continues to include the Feishu system config document.

## Self-Review

- Spec coverage: migrates backend T+ descriptions and inventory warehouse scope away from hardcoded-only behavior.
- Explicit deviation: field-option auto-update in Feishu is not implemented here because backend has T+ output but no Feishu write credentials, while doc-sync has Feishu credentials but no T+ output volume. Backend validates selected values against the T+ warehouse archive as the safety layer.
- Safety: no secret changes, no compose changes, and DB mirror read failures fall back to existing constants.
