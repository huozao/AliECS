# T+ 同步统一时间线 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 health 页的「同步请求」「执行记录」合并成一张统一编号(run id)的时间线表，每行可下载其产出的全量 BOM Excel；并让每次同步(含增量)都产出全量 BOM 版本+Excel，且仅在①改数值②增删/替换原料③删整条 BOM 时才标记需人工复核。

**Architecture:** 分两阶段。Phase A 改 tplus-sync-worker：增量也从 `tplus_bom_records` 拼出全量集 → 全量快照+全量 Excel；diff 收窄复核规则；产出文件名写进 `integration_sync_runs.detail_json.export_files`。Phase B 改 backend-api + public-web：新增 `/v1/ops/tplus/timeline` 合并 runs+孤儿请求并附 Excel(优先 detail_json、回退时间匹配)，health 页用一张表替换两张表、加列、弱化差异校验。

**Tech Stack:** Python(FastAPI, psycopg, pandas/openpyxl), 纯 HTML/JS(无框架), `unittest`/`pytest`。

**Spec:** `docs/superpowers/specs/2026-06-24-tplus-sync-timeline-unified-design.md`

**跨阶段契约（两阶段都依赖）：**
- `integration_sync_runs.detail_json.export_files`: `list[str]`，产出文件 basename，如 `["bom_20260624_100751.xlsx","current_stock_20260624_100752.xlsx"]`。
- `integration_sync_runs.detail_json.diff_summary`: `{"qty_changed":int,"material_changed":int,"bom_deleted":int,"bom_added":int,"status_changed":int,"cosmetic_changed":int,"needs_review":bool}`。
- 时间线行 `export_files` 渲染契约: `[{"name":str,"download_url":str|None,"pruned":bool}]`。

**运行测试：** 仓库根 `python -m pytest tests/ -q`；worker 包 `cd services/tplus-sync-worker && python -m pytest tests/ -q`（或根目录 `python -m pytest services/tplus-sync-worker/tests -q`）。

---

# Phase A — 同步语义（services/tplus-sync-worker）

### Task A1: item record_key 去掉 disabled（停用识别为状态变化而非删除+新增）

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py:283`
- Test: `services/tplus-sync-worker/tests/test_sync_state.py`

- [ ] **Step 1: 写失败测试**（追加到 `SyncStateTests`）

```python
    def test_toggling_disabled_is_a_field_change_not_remove_add(self):
        previous = snapshot_bom_rows(
            [{"Code": "P1", "Name": "成品1", "Version": "V1", "Disabled": "0",
              "BOMChilds": [{"ID": "1", "Code": "C1", "Name": "料1", "RequiredQuantity": 1, "Unit": {"Name": "kg"}}]}]
        )
        current = snapshot_bom_rows(
            [{"Code": "P1", "Name": "成品1", "Version": "V1", "Disabled": "1",
              "BOMChilds": [{"ID": "1", "Code": "C1", "Name": "料1", "RequiredQuantity": 1, "Unit": {"Name": "kg"}}]}]
        )
        previous["id"], current["id"] = 1, 2
        diff = build_snapshot_diff(previous=previous, current=current)
        assert diff is not None
        detail = diff["diff_json"]
        self.assertEqual(0, detail["added_count"])
        self.assertEqual(0, detail["removed_count"])
        self.assertEqual(1, detail["changed_count"])
        self.assertIn("disabled", detail["changed"][0]["changed_fields"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_sync_state.py::SyncStateTests::test_toggling_disabled_is_a_field_change_not_remove_add -v`
Expected: FAIL（当前 disabled 在 key 里 → added_count=1, removed_count=1）。

- [ ] **Step 3: 实现**——把 `_bom_item_from_parent_child` 里 record_key 的字段列表去掉 `"disabled"`：

`sync_state.py` 约 283 行，由
```python
        record_key = "|".join(str(key[name]) for name in ["parent_code", "version", "disabled", "child_code", "child_id"])
```
改为
```python
        record_key = "|".join(str(key[name]) for name in ["parent_code", "version", "child_code", "child_id"])
```
（`comparable["disabled"]` 保留不动，使其作为 changed_field 被检出。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_sync_state.py -v`
Expected: PASS（含原有用例；`test_build_snapshot_diff_includes_bom_item_level_changes` 仍绿）。

- [ ] **Step 5: 提交**

```bash
git add services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py services/tplus-sync-worker/tests/test_sync_state.py
git commit -m "fix(tplus): drop disabled from bom item key so toggling status is a field change"
```

---

### Task A2: diff 变化分类 + 收窄 needs_review（仅 改数值/增删替换原料/删BOM）

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py`（`build_snapshot_diff` + 新增 `classify_bom_changes`）
- Test: `services/tplus-sync-worker/tests/test_sync_state.py`

- [ ] **Step 1: 写失败测试**（追加）

```python
    def _snap(self, rows, sid):
        s = snapshot_bom_rows(rows)
        s["id"] = sid
        return s

    def test_qty_change_needs_review(self):
        prev = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C", "RequiredQuantity": 1}]}], 1)
        cur = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C", "RequiredQuantity": 2}]}], 2)
        diff = build_snapshot_diff(previous=prev, current=cur)
        c = diff["diff_json"]["classification"]
        self.assertEqual(1, c["qty_changed"])
        self.assertTrue(c["needs_review"])
        self.assertEqual("needs_review", diff["status"])

    def test_material_add_needs_review(self):
        prev = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C1", "RequiredQuantity": 1}]}], 1)
        cur = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C1", "RequiredQuantity": 1},
                          {"ID": "2", "Code": "C2", "RequiredQuantity": 1}]}], 2)
        c = build_snapshot_diff(previous=prev, current=cur)["diff_json"]["classification"]
        self.assertEqual(1, c["material_changed"])
        self.assertTrue(c["needs_review"])

    def test_bom_deletion_needs_review(self):
        prev = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C", "RequiredQuantity": 1}]}], 1)
        cur = self._snap([], 2)
        c = build_snapshot_diff(previous=prev, current=cur)["diff_json"]["classification"]
        self.assertEqual(1, c["bom_deleted"])
        self.assertTrue(c["needs_review"])

    def test_status_and_cosmetic_changes_are_informational(self):
        prev = self._snap([{"Code": "P", "Name": "旧名", "Version": "V", "Disabled": "0", "IsDefaultBom": "1",
            "BOMChilds": [{"ID": "1", "Code": "C", "Name": "料", "RequiredQuantity": 1, "Unit": {"Name": "kg"}}]}], 1)
        cur = self._snap([{"Code": "P", "Name": "新名", "Version": "V", "Disabled": "1", "IsDefaultBom": "0",
            "BOMChilds": [{"ID": "1", "Code": "C", "Name": "料改名", "RequiredQuantity": 1, "Unit": {"Name": "g"}}]}], 2)
        diff = build_snapshot_diff(previous=prev, current=cur)
        c = diff["diff_json"]["classification"]
        self.assertEqual(0, c["qty_changed"])
        self.assertEqual(0, c["material_changed"])
        self.assertEqual(0, c["bom_deleted"])
        self.assertFalse(c["needs_review"])
        self.assertEqual("informational", diff["status"])

    def test_new_bom_is_informational(self):
        prev = self._snap([], 1)
        cur = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C", "RequiredQuantity": 1}]}], 2)
        c = build_snapshot_diff(previous=prev, current=cur)["diff_json"]["classification"]
        self.assertEqual(1, c["bom_added"])
        self.assertFalse(c["needs_review"])
```

并**更新**原用例 `test_build_snapshot_diff_summarizes_changed_snapshot`（无 items，分类全 0 → 非复核）：
```python
    def test_build_snapshot_diff_summarizes_changed_snapshot(self):
        diff = build_snapshot_diff(
            previous={"id": 1, "row_count": 2, "snapshot_hash": "abc"},
            current={"id": 2, "row_count": 3, "snapshot_hash": "def"},
        )
        self.assertIsNotNone(diff)
        assert diff is not None
        self.assertEqual("informational", diff["status"])
        self.assertEqual(1, diff["diff_json"]["row_count_delta"])
        self.assertFalse(diff["diff_json"]["classification"]["needs_review"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_sync_state.py -v`
Expected: 新用例 FAIL（`classification` 键不存在）。

- [ ] **Step 3: 实现**——在 `sync_state.py` 新增纯函数 `classify_bom_changes` 并改 `build_snapshot_diff`。

新增（放在 `_diff_snapshot_items` 附近）：
```python
_PARENT_KEY_FIELDS = ("parent_code", "version")
_STATUS_FIELDS = {"disabled", "default_bom"}
_COSMETIC_FIELDS = {"parent_name", "child_name", "unit", "memo", "waste_rate"}


def _parent_key(item: Mapping[str, Any]) -> tuple[str, str]:
    return (str(item.get("parent_code") or ""), str(item.get("version") or ""))


def classify_bom_changes(previous_items: list[Any], current_items: list[Any]) -> dict[str, Any]:
    """把 item 级 diff 归类。needs_review 仅当: 改数值 / 增删替换原料 / 删整条 BOM。"""
    item_diff = _diff_snapshot_items(previous_items, current_items)
    prev_parents = {_parent_key(i) for i in previous_items}
    cur_parents = {_parent_key(i) for i in current_items}
    deleted_parents = prev_parents - cur_parents
    added_parents = cur_parents - prev_parents

    # 增删的 child 中，归属于"共存父件"的算原料种类变化；归属于新/删父件的算整条BOM增删。
    material_changed = 0
    for item in (item_diff.get("added") or []) + (item_diff.get("removed") or []):
        pk = _parent_key(item)
        if pk not in added_parents and pk not in deleted_parents:
            material_changed += 1

    qty_changed = 0
    status_changed = 0
    cosmetic_changed = 0
    for change in item_diff.get("changed") or []:
        fields = set(change.get("changed_fields") or [])
        if "quantity" in fields or "child_code" in fields:
            qty_changed += 1  # child_code 改变=换料,与数值并列计入需复核计数(见下 needs_review)
        elif fields and fields <= _STATUS_FIELDS:
            status_changed += 1
        elif fields and fields <= (_STATUS_FIELDS | _COSMETIC_FIELDS):
            cosmetic_changed += 1
        elif fields:
            cosmetic_changed += 1  # 其它非复核字段并入 informational

    # child_code 改变同时意味着换料(material)；为简洁仍计入 qty_changed 桶但触发复核。
    needs_review = qty_changed > 0 or material_changed > 0 or len(deleted_parents) > 0
    return {
        "qty_changed": qty_changed,
        "material_changed": material_changed,
        "bom_deleted": len(deleted_parents),
        "bom_added": len(added_parents),
        "status_changed": status_changed,
        "cosmetic_changed": cosmetic_changed,
        "needs_review": needs_review,
        **item_diff,
    }
```

改 `build_snapshot_diff`（替换整函数）：
```python
def build_snapshot_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any] | None:
    if previous is None or previous.get("snapshot_hash") == current.get("snapshot_hash"):
        return None
    previous_count = int(previous.get("row_count") or 0)
    current_count = int(current.get("row_count") or 0)
    classification = classify_bom_changes(previous.get("items") or [], current.get("items") or [])
    needs_review = bool(classification["needs_review"])
    return {
        "status": "needs_review" if needs_review else "informational",
        "severity": "warning" if needs_review else "info",
        "summary": f"BOM full snapshot changed: rows {previous_count} -> {current_count}",
        "diff_json": {
            "previous_snapshot_id": previous.get("id"),
            "current_snapshot_id": current.get("id"),
            "previous_hash": previous.get("snapshot_hash"),
            "current_hash": current.get("snapshot_hash"),
            "previous_row_count": previous_count,
            "current_row_count": current_count,
            "row_count_delta": current_count - previous_count,
            "classification": {k: classification[k] for k in (
                "qty_changed", "material_changed", "bom_deleted", "bom_added",
                "status_changed", "cosmetic_changed", "needs_review")},
            "added": classification.get("added", []),
            "removed": classification.get("removed", []),
            "changed": classification.get("changed", []),
            "added_count": classification.get("added_count", 0),
            "removed_count": classification.get("removed_count", 0),
            "changed_count": classification.get("changed_count", 0),
            "row_count_delta_items": classification.get("row_count_delta", 0),
        },
    }
```

> 注：`_diff_snapshot_items` 当前返回需含 `added/removed/changed/added_count/removed_count/changed_count`。先读该函数确认键名；若 `changed` 项的 `changed_fields` 不含 `child_code`，则 child 换料表现为该 item record_key 变化(removed+added)，已由 `material_changed` 捕获——`qty_changed` 分支里的 `child_code` 判断为冗余保护，可保留。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_sync_state.py -v`
Expected: PASS（全部）。

- [ ] **Step 5: 提交**

```bash
git add services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py services/tplus-sync-worker/tests/test_sync_state.py
git commit -m "feat(tplus): classify bom diff; needs_review only for qty/material/deletion"
```

---

### Task A3: `assemble_current_full_bom(conn)` — 从 tplus_bom_records 拼全量

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py`
- Test: `services/tplus-sync-worker/tests/test_sync_state.py`

- [ ] **Step 1: 写失败测试**（追加；用 fake conn，参考 backend 测试风格）

```python
class AssembleCurrentFullBomTests(unittest.TestCase):
    def test_dedupes_by_code_version_prefers_latest_seen(self):
        from tplus_datahub.jobs.sync_state import assemble_current_full_bom
        rows = [
            # (raw_json, last_seen_at) — 同一 (Code,Version) 两行，停用残留 + 新启用
            ({"Code": "P", "Version": "V", "Disabled": "1", "BOMChilds": []}, "2026-06-24 09:00"),
            ({"Code": "P", "Version": "V", "Disabled": "0", "BOMChilds": [{"Code": "C"}]}, "2026-06-24 10:00"),
            ({"Code": "Q", "Version": "V", "Disabled": "0", "BOMChilds": []}, "2026-06-24 08:00"),
        ]
        full = assemble_current_full_bom(_FakeBomRecordsConn(rows))
        codes = sorted((r["Code"], r["Version"], r["Disabled"]) for r in full)
        self.assertEqual([("P", "V", "0"), ("Q", "V", "0")], codes)


class _FakeBomRecordsConn:
    def __init__(self, rows):
        self._rows = rows
    def cursor(self):
        return _FakeBomRecordsCursor(self._rows)
    def close(self):
        pass


class _FakeBomRecordsCursor:
    def __init__(self, rows):
        self._rows = rows
        self._out = []
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def execute(self, sql, params=None):
        self._out = [(raw, seen) for raw, seen in self._rows]
    def fetchall(self):
        return self._out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_sync_state.py::AssembleCurrentFullBomTests -v`
Expected: FAIL（`assemble_current_full_bom` 不存在）。

- [ ] **Step 3: 实现**——在 `sync_state.py` 新增：

```python
def assemble_current_full_bom(conn: Any) -> list[Any]:
    """从 tplus_bom_records 取未失踪记录，按 (Code,Version) 去重(取 last_seen 最新)，返回原始行列表。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_json, last_seen_at
            FROM tplus_bom_records
            WHERE missing_since IS NULL
            ORDER BY last_seen_at ASC NULLS FIRST
            """
        )
        rows = cur.fetchall()
    by_key: dict[tuple[str, str], Any] = {}
    for raw, _seen in rows:
        record = raw if isinstance(raw, Mapping) else {}
        key = (str(record.get("Code") or record.get("code") or ""),
               str(record.get("Version") or record.get("version") or ""))
        by_key[key] = record  # ASC 排序 → 后写覆盖=最新 last_seen 胜出
    return list(by_key.values())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_sync_state.py::AssembleCurrentFullBomTests -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py services/tplus-sync-worker/tests/test_sync_state.py
git commit -m "feat(tplus): assemble_current_full_bom dedupes by code+version from db"
```

---

### Task A4: 统一"upsert→拼全量→全量快照→分类 diff→按需写复核"，返回全量行

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py`（新增 `upsert_and_snapshot_full_bom`，复用现有 `_upsert_tplus_bom_records`/`_latest_full_snapshot`）

无新单测（DB 写入路径，遵循现仓库不为 insert 写单测的惯例；分类逻辑已在 A2 覆盖）。改完后跑全 worker 测试确保未回归。

- [ ] **Step 1: 实现 `upsert_and_snapshot_full_bom`**

新增函数（与 `record_bom_snapshot_if_configured` 并列）：
```python
def upsert_and_snapshot_full_bom(
    fetched_rows: list[Any], *, mode: str, source_json: dict[str, Any] | None = None
) -> list[Any]:
    """upsert 抓到的行 → 从 DB 拼全量 → 写全量快照 → 与上一份全量快照分类 diff →
    仅当 needs_review 时写 reconciliation。返回用于导出的全量行(无 DB 时回退 fetched_rows)。"""
    if psycopg is None:
        return fetched_rows
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return fetched_rows
    try:
        with closing(psycopg.connect(database_url, connect_timeout=3)) as conn:
            with conn.cursor() as cur:
                _upsert_tplus_bom_records(cur, [_normalize_row(r) for r in fetched_rows])
            conn.commit()
            full_rows = assemble_current_full_bom(conn)
            snapshot = snapshot_bom_rows(full_rows)
            source = dict(source_json or {})
            source.update({"mode": mode, "snapshot_hash": snapshot["snapshot_hash"],
                           "records": snapshot["raw_records"], "items": snapshot["items"]})
            previous = _latest_full_snapshot(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO integration_sync_snapshots(provider, module, mode, row_count, snapshot_hash, source_json)
                    VALUES ('chanjet', 'bom', %s, %s, %s, %s) RETURNING id
                    """,
                    (mode, snapshot["row_count"], snapshot["snapshot_hash"], Jsonb(source)),
                )
                snapshot["id"] = int(cur.fetchone()[0])
                diff = build_snapshot_diff(previous, snapshot)
                if diff is not None and diff["status"] == "needs_review":
                    cur.execute(
                        """
                        INSERT INTO integration_reconciliation_diffs(
                            provider, module, status, severity, summary, diff_json,
                            full_snapshot_id, incremental_snapshot_id)
                        VALUES ('chanjet', 'bom', %s, %s, %s, %s, %s, NULL)
                        """,
                        (diff["status"], diff["severity"], diff["summary"], Jsonb(diff["diff_json"]), snapshot["id"]),
                    )
            conn.commit()
            return full_rows
    except Exception:
        return fetched_rows
```

> 注意 `_latest_full_snapshot(conn)` 当前查询限定 `mode IN ('full_bom','scheduled_full')` 之类——读其实现，改为取"最近一条 bom 全量快照(不限 mode)"，否则增量写的快照 diff 不到上一条。若它已是"最近 bom 快照"则不动。

- [ ] **Step 2: 跑全 worker 测试确认未回归**

Run: `cd services/tplus-sync-worker && python -m pytest tests/ -q`
Expected: PASS（A1-A3 用例 + 原有）。

- [ ] **Step 3: 提交**

```bash
git add services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py
git commit -m "feat(tplus): upsert_and_snapshot_full_bom builds full version + gated reconciliation"
```

---

### Task A5: job_sync_bom 导出全量并返回产出文件名

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_bom.py`
- Test: `services/tplus-sync-worker/tests/test_job_sync_bom.py`

- [ ] **Step 1: 写失败测试**（先读现有 test_job_sync_bom.py 的 mock 风格，追加）

```python
    def test_incremental_exports_assembled_full_and_returns_basename(self):
        import tplus_datahub.jobs.job_sync_bom as job
        from unittest.mock import patch
        with (
            patch.object(job, "load_settings", return_value="settings"),
            patch.object(job, "sync_bom", return_value=[{"Code": "P", "Version": "V"}]),
            patch.object(job, "upsert_and_snapshot_full_bom",
                         return_value=[{"Code": "P", "Version": "V"}, {"Code": "Q", "Version": "V"}]) as upsert,
            patch.object(job, "export_bom", return_value=__import__("pathlib").Path("/x/bom_20260624_100751.xlsx")) as export,
        ):
            result = job.run(target={"code": "P"}, mode="incremental")
        self.assertEqual(0, result.exit_code)
        self.assertEqual(["bom_20260624_100751.xlsx"], result.export_files)
        # 导出用的是拼出的全量(2 行)，非抓到的 1 行
        self.assertEqual(2, len(export.call_args.args[0]))
        upsert.assert_called_once()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_job_sync_bom.py -v`
Expected: FAIL（`job.run` / `upsert_and_snapshot_full_bom` 未引入）。

- [ ] **Step 3: 实现**——改 `job_sync_bom.py`：

顶部 import 改：
```python
from tplus_datahub.jobs.sync_state import upsert_and_snapshot_full_bom
```
（移除 `record_bom_snapshot_if_configured` 的 import。）

新增结果类型 + `run`，把 `main` 改为薄包装：
```python
from dataclasses import dataclass, field


@dataclass
class SyncBomResult:
    exit_code: int
    export_files: list[str] = field(default_factory=list)


def run(target: dict | None = None, mode: str = "full_bom") -> SyncBomResult:
    logger = get_logger("tplus_datahub.job_sync_bom", "output/logs/job_sync_bom.log")
    timestamp = now_timestamp()
    try:
        settings = load_settings()
        query_params = build_query_params_from_target(target)
        if mode == "incremental" and query_params:
            rows = sync_bom(settings=settings, timestamp=timestamp, query_params=query_params, include_disabled=True)
        else:
            rows = sync_bom(settings=settings, timestamp=timestamp)
        full_rows = upsert_and_snapshot_full_bom(rows, mode=mode, source_json={"target": target or {}})
        excel_path = export_bom(full_rows, settings=settings, timestamp=timestamp)
        logger.info("Excel 已导出(全量 %s 行)：%s", len(full_rows), excel_path)
        return SyncBomResult(exit_code=0, export_files=[excel_path.name])
    except ConfigError as exc:
        logger.error("配置错误：%s", exc); return SyncBomResult(2)
    except ChanjetAPIError as exc:
        logger.error("接口错误：endpoint=%s status=%s body=%s", exc.endpoint, exc.status_code, text_preview(exc.body_preview)); return SyncBomResult(3)
    except TPlusDataHubError as exc:
        logger.error("同步失败：%s", exc); return SyncBomResult(4)
    except Exception as exc:
        logger.exception("未知异常：%s", exc); return SyncBomResult(1)


def main(target: dict | None = None, mode: str = "full_bom") -> int:
    return run(target=target, mode=mode).exit_code
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_job_sync_bom.py -v`
Expected: PASS（原 main 用例仍绿，因 main 行为不变）。

- [ ] **Step 5: 提交**

```bash
git add services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_bom.py services/tplus-sync-worker/tests/test_job_sync_bom.py
git commit -m "feat(tplus): job_sync_bom exports assembled full bom and returns produced filename"
```

---

### Task A6: job_sync_all 收集各模块产出 basename

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_all.py`
- Test: `services/tplus-sync-worker/tests/test_job_sync_all.py`

- [ ] **Step 1: 写失败测试**（追加；沿用现有 patch 块，断言 `run()` 返回 basename 列表）

```python
    def test_run_collects_export_basenames(self):
        import tplus_datahub.jobs.job_sync_all as job
        from pathlib import Path
        with (
            patch.object(job, "load_settings", return_value="settings"),
            patch.object(job, "sync_bom", return_value=[]),
            patch.object(job, "upsert_and_snapshot_full_bom", return_value=[], create=True),
            patch.object(job, "export_bom", return_value=Path("/x/bom_20260624_100751.xlsx")),
            patch.object(job, "sync_inventory", return_value=[]),
            patch.object(job, "export_inventory", return_value=Path("/x/current_stock_20260624_100752.xlsx")),
            patch.object(job, "sync_partner", return_value=[]),
            patch.object(job, "export_partner", return_value=Path("/x/partner_20260624_100753.xlsx")),
            patch.object(job, "VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS", {}, create=True),
            patch.object(job, "VERIFIED_VOUCHER_LIST_ENDPOINTS", {}, create=True),
            patch.object(job, "sync_purchase_price", return_value=[]),
            patch.object(job, "export_purchase_price", return_value=Path("/x/purchase_price_20260624_100754.xlsx")),
            patch.object(job, "sync_sales_price", return_value=[]),
            patch.object(job, "export_sales_price", return_value=Path("/x/sales_price_20260624_100755.xlsx")),
        ):
            result = job.run()
        self.assertEqual(0, result.exit_code)
        self.assertIn("bom_20260624_100751.xlsx", result.export_files)
        self.assertIn("current_stock_20260624_100752.xlsx", result.export_files)
        self.assertEqual(5, len(result.export_files))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_job_sync_all.py -v`
Expected: FAIL（`job.run` 不存在）。

- [ ] **Step 3: 实现**——改 `job_sync_all.py`：

import 处把 `record_bom_snapshot_if_configured` 换成 `upsert_and_snapshot_full_bom`，新增结果类型，把 `main` 改薄包装，`run()` 收集 basename。关键改动：

```python
from dataclasses import dataclass, field
from pathlib import Path
from tplus_datahub.jobs.sync_state import upsert_and_snapshot_full_bom


@dataclass
class SyncAllResult:
    exit_code: int
    export_files: list[str] = field(default_factory=list)


def _basename(path: object) -> str:
    return Path(str(path)).name


def run() -> SyncAllResult:
    logger = get_logger("tplus_datahub.job_sync_all", "output/logs/job_sync_all.log")
    timestamp = now_timestamp()
    exports: list[str] = []
    try:
        settings = load_settings()

        bom_rows = sync_bom(settings=settings, timestamp=timestamp)
        full_rows = upsert_and_snapshot_full_bom(bom_rows, mode="scheduled_full", source_json={"job": "job_sync_all"})
        bom_path = export_bom(full_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(bom_path)); logger.info("BOM Excel exported: %s", bom_path)

        inventory_rows = sync_inventory(settings=settings, timestamp=timestamp)
        inventory_path = export_inventory(inventory_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(inventory_path)); logger.info("Inventory Excel exported: %s", inventory_path)

        partner_rows = sync_partner(settings=settings, timestamp=timestamp)
        partner_path = export_partner(partner_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(partner_path)); logger.info("Partner Excel exported: %s", partner_path)

        for module_name, endpoint in VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS.items():
            archive_rows = sync_base_archive(module_name=module_name, endpoint=endpoint, settings=settings, timestamp=timestamp)
            archive_path = export_base_archive(module_name, archive_rows, settings=settings, timestamp=timestamp)
            exports.append(_basename(archive_path)); logger.info("%s Excel exported: %s", module_name, archive_path)

        for module_name, config in VERIFIED_VOUCHER_LIST_ENDPOINTS.items():
            voucher_rows = sync_voucher_list(module_name=module_name, endpoint=config["endpoint"], select_fields=config["select_fields"], settings=settings, timestamp=timestamp)
            voucher_path = export_voucher_list(module_name, voucher_rows, settings=settings, timestamp=timestamp)
            exports.append(_basename(voucher_path)); logger.info("%s Excel exported: %s", module_name, voucher_path)

        purchase_price_rows = sync_purchase_price(settings=settings, timestamp=timestamp)
        purchase_price_path = export_purchase_price(purchase_price_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(purchase_price_path)); logger.info("purchase_price Excel exported: %s", purchase_price_path)

        sales_price_rows = sync_sales_price(settings=settings, timestamp=timestamp)
        sales_price_path = export_sales_price(sales_price_rows, settings=settings, timestamp=timestamp)
        exports.append(_basename(sales_price_path)); logger.info("sales_price Excel exported: %s", sales_price_path)

        for module_name in PENDING_MODULES:
            logger.info("%s module endpoint is not confirmed; skipped", module_name)
        return SyncAllResult(0, exports)
    except ConfigError as exc:
        logger.error("Config error: %s", exc); return SyncAllResult(2, exports)
    except ChanjetAPIError as exc:
        logger.error("API error: endpoint=%s status=%s body=%s", exc.endpoint, exc.status_code, text_preview(exc.body_preview)); return SyncAllResult(3, exports)
    except TPlusDataHubError as exc:
        logger.error("Sync failed: %s", exc); return SyncAllResult(4, exports)
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc); return SyncAllResult(1, exports)


def main() -> int:
    return run().exit_code
```

> 原 `test_main_syncs_verified_base_archives_after_core_modules` 用 `main()` 且 mock 返回字符串 `"bom.xlsx"` 等——`main()` 仍存在且行为(exit code)不变，该测试保持绿；`_basename("bom.xlsx")` = `"bom.xlsx"` 不报错。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_job_sync_all.py -v`
Expected: PASS（新 `run` 用例 + 原 `main` 用例）。

- [ ] **Step 5: 提交**

```bash
git add services/tplus-sync-worker/src/tplus_datahub/jobs/job_sync_all.py services/tplus-sync-worker/tests/test_job_sync_all.py
git commit -m "feat(tplus): job_sync_all.run collects produced excel basenames"
```

---

### Task A7: worker_loop 把 export_files 写进 run 的 detail_json（定时 + 手动/DB）

**Files:**
- Modify: `services/tplus-sync-worker/src/tplus_datahub/jobs/worker_loop.py`
- Test: `services/tplus-sync-worker/tests/test_worker_loop.py`

- [ ] **Step 1: 写失败测试**（先读 test_worker_loop.py 现有注入风格；追加两个）

```python
    def test_scheduled_run_records_export_files_from_result(self):
        import tplus_datahub.jobs.worker_loop as wl
        from tplus_datahub.jobs.job_sync_all import SyncAllResult
        recorded = {}
        def fake_record(**kwargs):
            recorded.update(kwargs); return 1
        wl.run_forever(
            sync_once=lambda: SyncAllResult(0, ["bom_20260624_100751.xlsx", "current_stock_x.xlsx"]),
            record_sync_run=fake_record,
            sleep=lambda s: None,
            max_runs=1,
        )
        self.assertEqual(["bom_20260624_100751.xlsx", "current_stock_x.xlsx"],
                         recorded["detail_json"]["export_files"])

    def test_scheduled_run_tolerates_int_sync_once(self):
        import tplus_datahub.jobs.worker_loop as wl
        recorded = {}
        wl.run_forever(sync_once=lambda: 0, record_sync_run=lambda **k: recorded.update(k) or 1,
                       sleep=lambda s: None, max_runs=1)
        self.assertEqual([], recorded["detail_json"]["export_files"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/tplus-sync-worker && python -m pytest tests/test_worker_loop.py -v`
Expected: FAIL（detail_json 现为 `{"run": run_count}` 无 export_files）。

- [ ] **Step 3: 实现**——改 `run_forever` 内主循环（worker_loop.py 约 156-181）。

把 `last_exit_code = int(sync_once() or 0)` 段替换为归一化 outcome：
```python
        try:
            outcome = sync_once()
        except Exception:
            logger.exception("T+ sync run failed with unexpected exception: run=%s", run_count)
            outcome = 1
        if hasattr(outcome, "exit_code"):
            last_exit_code = int(outcome.exit_code or 0)
            export_files = list(getattr(outcome, "export_files", []) or [])
        else:
            last_exit_code = int(outcome or 0)
            export_files = []
```
并把 `record_sync_run(...)` 的 `detail_json` 改为：
```python
                detail_json={"run": run_count, "export_files": export_files},
```
同时把默认 `sync_once` 改为返回 `SyncAllResult` 的入口：顶部 import 改
```python
from tplus_datahub.jobs.job_sync_all import run as sync_all_run
```
并把 `run_forever(*, sync_once: Callable[[], int | None] = sync_all_main, ...)` 默认值改为 `sync_once: Callable[[], Any] = sync_all_run`（`from typing import Any`）。

手动/DB 路径的 export_files：把 `sync_bom_request_once` 默认 lambda 改用 `job_sync_bom.run` 并把 basename 塞进 detail。即 `_run_pending_db_bom_request` 内：
```python
    try:
        result = sync_bom_request_once(request)  # 现在返回 SyncBomResult
        exit_code = int(getattr(result, "exit_code", result) or 0)
        export_files = list(getattr(result, "export_files", []) or [])
    except Exception as exc:
        ...
        exit_code = 1; export_files = []
        detail = {"error": str(exc), "mode": request.get("mode"), "target_json": request.get("target_json") or {}}
    else:
        detail = {"mode": request.get("mode"), "target_json": request.get("target_json") or {}, "export_files": export_files}
```
并把 `run_forever` 默认 `sync_bom_request_once` 改为：
```python
    sync_bom_request_once: Callable[[dict], Any] = lambda request: sync_bom_run(
        target=request.get("target_json") or {}, mode=str(request.get("mode") or "incremental")),
```
顶部 `from tplus_datahub.jobs.job_sync_bom import run as sync_bom_run`（保留 `main as sync_bom_main` 供文件式 `sync_bom_once` 用）。

> `finish_bom_request`(db_sync_requests.py) 已把传入 `detail` 整个写进 `detail_json`，故 `detail["export_files"]` 自动落库，无需改 db_sync_requests。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/tplus-sync-worker && python -m pytest tests/ -q`
Expected: PASS（全 worker 测试）。

- [ ] **Step 5: 提交**

```bash
git add services/tplus-sync-worker/src/tplus_datahub/jobs/worker_loop.py services/tplus-sync-worker/tests/test_worker_loop.py
git commit -m "feat(tplus): record produced export_files into sync run detail_json"
```

**Phase A 收尾：** `python -m pytest services/tplus-sync-worker/tests -q` 全绿后进入 Phase B。

---

# Phase B — 统一时间线（backend-api + public-web）

### Task B1: backend Excel↔run 时间匹配 helper

**Files:**
- Modify: `services/backend-api/app/main.py`
- Test: `tests/test_backend_exports.py`

- [ ] **Step 1: 写失败测试**（追加到 test_backend_exports.py；先读其 import/setup 风格）

```python
    def test_match_export_files_buckets_to_first_run_at_or_after_file_time(self):
        from app.main import _match_export_files_to_runs
        # runs: (id, finished_at_iso) 倒序
        runs = [(252, "2026-06-24T10:10:00"), (251, "2026-06-24T10:08:00"), (250, "2026-06-24T09:00:00")]
        files = ["bom_20260624_100751.xlsx", "current_stock_20260624_100752.xlsx", "bom_20260624_085500.xlsx"]
        mapping = _match_export_files_to_runs(runs, files)
        self.assertEqual(["bom_20260624_100751.xlsx", "current_stock_20260624_100752.xlsx"],
                         sorted(mapping[251]))
        self.assertEqual(["bom_20260624_085500.xlsx"], mapping[250])
        self.assertNotIn(252, mapping)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backend_exports.py -v -k match_export`
Expected: FAIL（函数不存在）。

- [ ] **Step 3: 实现**——在 `main.py`（`_tplus_module_of` 附近）新增：

```python
def _parse_export_timestamp(file_name: str) -> datetime | None:
    stem = file_name[:-5] if file_name.endswith(".xlsx") else file_name
    parts = stem.rsplit("_", 2)
    if len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) == 6 and parts[1].isdigit() and parts[2].isdigit():
        try:
            return datetime.strptime(parts[1] + parts[2], "%Y%m%d%H%M%S")
        except ValueError:
            return None
    return None


def _match_export_files_to_runs(runs: list[tuple[Any, Any]], files: list[str]) -> dict[Any, list[str]]:
    """把每个文件归给 finished_at >= 文件时间戳 的最早一次 run。
    runs: [(run_id, finished_at_iso_or_dt)]，可乱序。返回 {run_id: [file,...]}。"""
    parsed_runs = []
    for run_id, finished in runs:
        if finished is None:
            continue
        dt = finished if isinstance(finished, datetime) else datetime.fromisoformat(str(finished).replace("Z", "")[:19])
        parsed_runs.append((dt, run_id))
    parsed_runs.sort()  # 按时间升序
    mapping: dict[Any, list[str]] = {}
    for name in files:
        t = _parse_export_timestamp(name)
        if t is None:
            continue
        chosen = next((rid for dt, rid in parsed_runs if dt >= t), None)
        if chosen is not None:
            mapping.setdefault(chosen, []).append(name)
    return mapping
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backend_exports.py -v -k match_export`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/backend-api/app/main.py tests/test_backend_exports.py
git commit -m "feat(backend): time-window matcher mapping tplus export files to sync runs"
```

---

### Task B2: backend `/v1/ops/tplus/timeline` 合并端点

**Files:**
- Modify: `services/backend-api/app/main.py`
- Test: `tests/test_backend_ops_status.py`

- [ ] **Step 1: 写失败测试**（追加到 BackendOpsStatusTests，fake conn 直调端点）

```python
    def test_tplus_timeline_merges_runs_and_orphan_requests(self) -> None:
        from app import main as main_module
        old_conn = main_module._conn
        main_module._conn = lambda: _FakeTimelineConn()
        old_dir = main_module._tplus_export_dir
        main_module._tplus_export_dir = lambda: __import__("pathlib").Path("/nonexistent")
        try:
            result = main_module.ops_tplus_timeline(limit=20, offset=0, _={})
        finally:
            main_module._conn = old_conn
            main_module._tplus_export_dir = old_dir
        kinds = [row["kind"] for row in result["items"]]
        self.assertIn("run", kinds)
        self.assertIn("request", kinds)
        run_row = next(r for r in result["items"] if r["kind"] == "run")
        self.assertEqual("#251", run_row["number"])
        self.assertEqual(["bom_20260624_100751.xlsx"], [f["name"] for f in run_row["export_files"]])
        self.assertTrue(run_row["export_files"][0]["pruned"])  # 目录不存在 → 不可下载
        self.assertTrue(run_row["needs_review"])
        req_row = next(r for r in result["items"] if r["kind"] == "request")
        self.assertEqual("请求·R58", req_row["number"])
        self.assertEqual(2, result["total"])
```

并在文件末尾追加 `_FakeTimelineConn`/`_FakeTimelineCursor`（按端点 SQL 前缀返回）：
```python
class _FakeTimelineConn:
    def cursor(self): return _FakeTimelineCursor()
    def close(self): pass


class _FakeTimelineCursor:
    def __init__(self): self._rows = []; self._one = None
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def execute(self, sql, params=None):
        n = " ".join(sql.lower().split())
        self._rows = []; self._one = None
        if n.startswith("select count(*)"):
            self._one = (2,); return
        if "union all" in n:
            # (kind, id, module, mode, status, event_time, row_count, exit_code,
            #  reason_event_id, request_id, detail_json)
            self._rows = [
                ("run", 251, "all", "scheduled_full", "success", "2026-06-24T10:08:00", 800, 0,
                 None, None, {"run": 5, "export_files": ["bom_20260624_100751.xlsx"],
                              "diff_summary": {"qty_changed": 1, "needs_review": True}}),
                ("request", 58, "bom", "incremental", "pending", "2026-06-24T10:05:00", None, None,
                 "evt-1", 58, {}),
            ]; return
        raise AssertionError(f"unexpected SQL: {sql}")
    def fetchall(self): return self._rows
    def fetchone(self): return self._one
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backend_ops_status.py -v -k timeline`
Expected: FAIL（`ops_tplus_timeline` 不存在）。

- [ ] **Step 3: 实现**——在 `main.py` 新增端点（紧挨 `ops_tplus_requests` 之后）：

```python
@app.get("/v1/ops/tplus/timeline")
def ops_tplus_timeline(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """统一时间线：执行(run) + 无执行的孤儿请求，按时间倒序分页；附产出 Excel 与变化摘要。"""
    items: list[dict[str, Any]] = []
    total = 0
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT (SELECT COUNT(*) FROM integration_sync_runs WHERE provider='chanjet')
                         + (SELECT COUNT(*) FROM integration_sync_requests WHERE provider='chanjet' AND sync_run_id IS NULL)
                    """
                )
                total = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT kind, id, module, mode, status, event_time, row_count, exit_code,
                           reason_event_id, request_id, detail_json
                    FROM (
                        SELECT 'run' AS kind, sr.id AS id, sr.module, sr.mode, sr.status,
                               sr.finished_at AS event_time, sr.row_count, sr.exit_code,
                               req.reason_event_id, req.id AS request_id, sr.detail_json
                        FROM integration_sync_runs sr
                        LEFT JOIN LATERAL (
                            SELECT id, reason_event_id FROM integration_sync_requests
                            WHERE provider='chanjet' AND sync_run_id = sr.id
                            ORDER BY requested_at DESC NULLS LAST, id DESC LIMIT 1
                        ) req ON TRUE
                        WHERE sr.provider='chanjet'
                        UNION ALL
                        SELECT 'request' AS kind, r.id, r.module, r.mode, r.status,
                               r.requested_at AS event_time, NULL::int, NULL::int,
                               r.reason_event_id, r.id, r.error_json
                        FROM integration_sync_requests r
                        WHERE r.provider='chanjet' AND r.sync_run_id IS NULL
                    ) merged
                    ORDER BY event_time DESC NULLS LAST, kind, id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取 T+ 时间线失败：{type(exc).__name__}") from exc

    # 收集需要回退时间匹配的 run(detail_json 无 export_files)
    run_rows = [(r[1], r[5]) for r in rows if r[0] == "run"]
    on_disk = _tplus_export_dir()
    disk_files = [p.name for p in on_disk.glob("*.xlsx")] if on_disk.is_dir() else []
    fallback = _match_export_files_to_runs(run_rows, disk_files)
    existing = set(disk_files)

    for kind, rid, module, mode, status, event_time, row_count, exit_code, reason_event_id, request_id, detail in rows:
        detail = _json_value(detail) or {}
        row: dict[str, Any] = {
            "kind": kind,
            "number": f"#{rid}" if kind == "run" else f"请求·R{rid}",
            "id": rid, "module": module, "mode": mode, "status": status,
            "event_time": str(event_time) if event_time else None,
            "row_count": row_count, "exit_code": exit_code,
            "reason_event_id": reason_event_id, "request_id": request_id,
            "diff_summary": detail.get("diff_summary"),
            "needs_review": bool((detail.get("diff_summary") or {}).get("needs_review")),
            "export_files": [],
        }
        if kind == "run":
            names = list(detail.get("export_files") or []) or fallback.get(rid, [])
            row["export_files"] = [
                {"name": name,
                 "download_url": f"/v1/exports/tplus/{name}" if name in existing else None,
                 "pruned": name not in existing}
                for name in names
            ]
        items.append(row)
    return {"items": items, "total": total, "limit": limit, "offset": offset}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backend_ops_status.py -v -k timeline`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add services/backend-api/app/main.py tests/test_backend_ops_status.py
git commit -m "feat(backend): /v1/ops/tplus/timeline merges runs+orphan requests with excel + diff summary"
```

---

### Task B3: health 页用一张统一表替换两张表 + 新列 + 下载 + 行内复核

**Files:**
- Modify: `services/public-web/health/index.html`

无独立单测步骤（断言在 Task B4）。本任务改 HTML/JS。

- [ ] **Step 1: 改 DOM**——把 `tplusSyncModule` 区块（约 26-34 行）内"同步请求"+"执行记录（全部）"两块替换为单表容器：

```html
      <section class="band" id="tplusSyncModule">
        <h2>T+ 同步</h2>
        <p class="muted">统一时间线：每次同步(执行)按 <b>#编号</b> 列出，与配方页「同步 #N」对应；含本次变化与产出的全量 BOM Excel(可下载核对)。来源：手动同步、定时同步、订阅变更同步。</p>
        <div class="row" style="gap:8px;margin-bottom:10px"><span class="chip degraded">手动同步</span><span class="chip ok">定时同步</span><span class="chip warning">订阅变更同步</span></div>
        <div id="tplusTimeline"></div><div id="tplusTimelinePager" class="row" style="gap:10px;align-items:center;margin-top:8px"></div>
      </section>
```

- [ ] **Step 2: 改 JS**——删除 `loadTplusRequests/renderTplus/loadTplusRuns/renderTplusRuns` 四个函数（约 116-149 行）及 `tplusReqItems/tplusReqOffset/tplusRunsOffset` 相关；`openTplusRequestDetail` 改造为通用行详情。新增时间线渲染：

```javascript
    let tplusTlOffset=0; const TPLUS_TL_LIMIT=20; let tplusTlItems=[];
    async function loadTplusTimeline(){
      try{
        const d=await api(`/v1/ops/tplus/timeline?limit=${TPLUS_TL_LIMIT}&offset=${tplusTlOffset}`);
        renderTplusTimeline(d);
      }catch(e){$('tplusTimeline').innerHTML=`<span class="chip failed">时间线加载失败：${esc(e.message)}</span>`;$('tplusTimelinePager').innerHTML='';}
    }
    function diffSummaryText(s){
      if(!s)return '<span class="muted">—</span>';
      const parts=[];
      if(s.qty_changed)parts.push(`改数值${s.qty_changed}`);
      if(s.material_changed)parts.push(`增删料${s.material_changed}`);
      if(s.bom_deleted)parts.push(`删BOM${s.bom_deleted}`);
      if(s.bom_added)parts.push(`新增${s.bom_added}`);
      if(s.status_changed)parts.push(`状态${s.status_changed}`);
      if(s.cosmetic_changed)parts.push(`其它${s.cosmetic_changed}`);
      if(!parts.length)return '<span class="muted">无变化</span>';
      const txt=parts.join(' · ');
      return s.needs_review?`<span class="chip failed">需复核</span> ${esc(txt)}`:esc(txt);
    }
    function excelCell(files){
      if(!files||!files.length)return '<span class="muted">—</span>';
      return files.map((f)=>f.pruned
        ?`<span class="chip" title="已超出保留期被清理">${esc(f.name)}（已清理）</span>`
        :`<button class="btn primary" type="button" onclick="downloadExport('${esc(f.download_url)}','${esc(f.name)}')">${esc(f.name)}</button>`
      ).join(' ');
    }
    function renderTplusTimeline(d){
      tplusTlItems=d.items||[];
      const rows=tplusTlItems.map((it,index)=>{
        const numCell=it.kind==='run'?`<b>${esc(it.number)}</b>`:`<span class="muted">${esc(it.number)}</span>`;
        const trCls=it.needs_review?' style="background:#f9eceb"':'';
        return `<tr${trCls}><td>${numCell}</td><td>${syncOriginLabel(it)}</td><td>${esc(it.module)}</td><td>${esc(it.mode)}</td><td>${chip(it.status)}</td><td>${fmtTime(it.event_time)}</td><td>${esc(it.row_count??'')}</td><td>${esc(it.exit_code??'')}</td><td>${esc(it.reason_event_id||'')}</td><td>${diffSummaryText(it.diff_summary)}</td><td>${excelCell(it.export_files)}</td><td><button class="btn primary" type="button" onclick="openTimelineDetail(${index})">详情</button></td></tr>`;
      }).join('');
      $('tplusTimeline').innerHTML=`<table><thead><tr><th>编号</th><th>来源</th><th>模块</th><th>模式</th><th>状态</th><th>时间</th><th>行数</th><th>退出码</th><th>回调事件ID</th><th>本次变化</th><th>生成的 Excel</th><th>详情</th></tr></thead><tbody>${rows||'<tr><td colspan="12" class="muted">暂无记录</td></tr>'}</tbody></table>`;
      const total=d.total||0,limit=d.limit||TPLUS_TL_LIMIT,offset=d.offset||0;
      const page=Math.floor(offset/limit)+1,pages=Math.max(1,Math.ceil(total/limit));
      $('tplusTimelinePager').innerHTML=`<button class="btn" type="button" id="tplusTlPrev" ${offset<=0?'disabled':''}>上一页</button><span class="muted">第 ${page}/${pages} 页 · 共 ${total} 条</span><button class="btn" type="button" id="tplusTlNext" ${offset+limit>=total?'disabled':''}>下一页</button>`;
      const prev=$('tplusTlPrev'),next=$('tplusTlNext');
      if(prev)prev.onclick=()=>{tplusTlOffset=Math.max(0,tplusTlOffset-TPLUS_TL_LIMIT);loadTplusTimeline();};
      if(next)next.onclick=()=>{tplusTlOffset=tplusTlOffset+TPLUS_TL_LIMIT;loadTplusTimeline();};
    }
    function openTimelineDetail(index){
      const it=tplusTlItems[index];if(!it)return;
      const head=`<p class="muted">${esc(it.number)} · ${esc(it.module)}/${esc(it.mode)}/${esc(it.status)} · ${fmtTime(it.event_time)} · 请求ID ${esc(it.request_id||'-')} · 事件 ${esc(it.reason_event_id||'-')}</p>`;
      const files=(it.export_files||[]).length?`<p>产出 Excel：${excelCell(it.export_files)}</p>`:'';
      const review=it.needs_review&&it.reconciliation_id
        ?`<div class="row" style="margin-top:10px"><button class="btn primary" onclick="resolveRecon(${it.reconciliation_id},'use_current')">采用当前</button><button class="btn" onclick="resolveRecon(${it.reconciliation_id},'use_previous')">保留上一</button><button class="btn" onclick="resolveRecon(${it.reconciliation_id},'ignore')">忽略</button></div>`:'';
      openModal(`同步详情 ${it.number}`,`${head}${files}${jsonDetailBlock('变化摘要',it.diff_summary)}${review}`);
    }
```

> `it.reconciliation_id` 由 Task B2 端点附带：在 B2 SQL/组装里为 needs_review 的 run 额外查 `integration_reconciliation_diffs.id`（最近一条 status='needs_review' 且 full_snapshot_id 属该 run 的快照）。若实现期判定复杂，可先省略行内复核动作（仅展示摘要），复核仍可走保留的 reconciliation 端点——但 spec 要求行内入口，故在 B2 端点 run 分支补 `row["reconciliation_id"]`（见下方 B2 补充）。

- [ ] **Step 3: 改 refresh()**——把 `loadTplusRequests();loadTplusRuns();` 两行替换为 `loadTplusTimeline();`（约 272-273 行）。

- [ ] **Step 4: 弱化差异校验区块**——把 `<section class="band"><h2>差异校验</h2>...` 整段移除（约 35 行）；`renderRecon` 调用从 `refresh()` 移除（约 274 行）。`reconciliation` 后端端点与 `openRecon/resolveRecon` 保留（行内复核复用 `resolveRecon`）。

- [ ] **Step 5: 提交**

```bash
git add services/public-web/health/index.html
git commit -m "feat(health): unified T+ sync timeline table with excel download + inline review"
```

---

### Task B2 补充（在 B3 之前或并入 B2）：端点附 reconciliation_id

为支持行内复核入口，B2 端点 run 分支需带 `reconciliation_id`。在 B2 SQL 的 `'run'` 子查询里追加 LATERAL：
```sql
                        LEFT JOIN LATERAL (
                            SELECT d.id FROM integration_reconciliation_diffs d
                            JOIN integration_sync_snapshots s ON s.id = d.full_snapshot_id
                            WHERE d.provider='chanjet' AND d.status='needs_review'
                              AND s.created_at <= sr.finished_at
                            ORDER BY s.created_at DESC LIMIT 1
                        ) rec ON TRUE
```
并在 SELECT 增加 `rec.id AS reconciliation_id`（request 分支补 `NULL`），组装时 `row["reconciliation_id"]=...`。为该列补一条断言到 B2 测试（fake 返回某 id → run_row["reconciliation_id"] 命中）。

> 该关联是启发式(按快照时间≤run完成时间取最近需复核)；若实现期发现误配率高，回退为「不带行内动作、仅高亮+摘要」，并在 spec 风险节注明。

---

### Task B4: 前端断言（test_health_frontend.py）

**Files:**
- Test: `tests/test_health_frontend.py`

- [ ] **Step 1: 写测试**（替换/追加；反映新结构）

```python
    def test_unified_timeline_replaces_two_tables(self) -> None:
        self.assertIn('<section class="band" id="tplusSyncModule">', self.html)
        self.assertIn("<h2>T+ 同步</h2>", self.html)
        self.assertIn("function loadTplusTimeline(", self.html)
        self.assertIn("/v1/ops/tplus/timeline", self.html)
        self.assertIn("<th>生成的 Excel</th>", self.html)
        self.assertIn("<th>本次变化</th>", self.html)
        self.assertIn("function excelCell(", self.html)
        self.assertIn("function diffSummaryText(", self.html)
        # 旧的分离函数已移除
        self.assertNotIn("function loadTplusRuns(", self.html)
        self.assertNotIn("function loadTplusRequests(", self.html)
        # 差异校验大区块已弱化(移除独立标题)
        self.assertNotIn("<h2>差异校验</h2>", self.html)
```

并删除/更新原 `test_tplus_recent_requests_have_detail_entry`、`test_tplus_requests_and_runs_are_one_labeled_module` 中对已删函数(`openTplusRequestDetail`、"执行记录")的断言，改为断言 `openTimelineDetail` 与 `syncOriginLabel/手动同步/定时同步/订阅变更同步` 仍在。

- [ ] **Step 2: 跑测试确认通过**

Run: `python -m pytest tests/test_health_frontend.py -v`
Expected: PASS。

- [ ] **Step 3: 全量回归**

Run: `python -m pytest tests/ -q && python -m pytest services/tplus-sync-worker/tests -q`
Expected: PASS（全绿）。

- [ ] **Step 4: 提交**

```bash
git add tests/test_health_frontend.py
git commit -m "test(health): assert unified timeline structure and removed legacy tables"
```

---

## 部署与收尾

- 开 PR：`feature/tplus-sync-timeline-unified` → main。PR 描述列明 Phase A(worker 行为变更，需 rebuild tplus-sync-worker 镜像并部署)与 Phase B(backend/public-web 随 release 部署)。
- 合并后部署：worker 镜像更新最关键（增量现产全量 Excel + 收窄复核）；backend/public-web 走常规 release。
- 真机验证：触发一次订阅增量 → /health/ 时间线出现该 run、`生成的 Excel` 可下载、状态变化不报「需复核」；改一个 BOM 数值 → 该 run 行高亮「需复核」、行内可处理；/formula/ 查询「同步 #N」与时间线 `#N` 行及其 Excel 一致。

## Self-Review 结论
- Spec 覆盖：D1-D6 与四目标均有任务对应（D1/D2→B2;D3→A7+B1+B2;D4→A6+B2;D5→A3+A4+A5;D6→A1+A2;目标4→B3 Step4）。
- 跨任务类型一致：`SyncBomResult`/`SyncAllResult.export_files`、`detail_json.export_files`/`diff_summary`、时间线 `export_files` 渲染契约在 A5/A6/A7/B2/B3 一致。
- 占位符：无 TODO；B2 补充的 reconciliation_id 关联给了 SQL 与回退方案。
