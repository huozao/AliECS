# 标准型号色彩空间：补建行 + 标签与设置重组 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 T+ 全部有效父件自动补建进企微「标准型号0117」，并把 `/formula/colors/` 的色点标签换成可全量显示、按距离淡化的 DOM 层，同时把散落的视图设置收进顶部面板。

**Architecture:** 两个互不依赖的 PR。PR-A 只改 `doc-sync-worker` 的一个管道文件，加"补建缺失行"阶段。PR-B 只改 `public-web` 的单个 HTML 文件，把基于 canvas Sprite 的单点标签换成绝对定位 DOM 层，并重组设置 DOM。

**Tech Stack:** Python 3 + unittest（后端）；three.js 0.180 + camera-controls 2.10.1，单文件内联 ES module（前端）；前端测试是对 HTML 源码的字符串断言，不跑浏览器。

**设计依据:** `docs/superpowers/specs/2026-08-06-formula-colors-sync-and-view-design.md`

## Global Constraints

- AliECS 变更走分支 + PR，**不直推 main**。所有写 `.git` 的命令串行执行。
- 每次 commit 前跑 `python -m unittest discover -s tests`（在 `AliECS/` 根目录），全绿才提交。
- 前端**不新增任何依赖**。importmap 只允许现有的 `three`、`three/addons/`、`camera-controls` 三项，不引入 `CSS2DRenderer`。
- 容差盒重叠判定与容差内判定必须走 `toleranceRange(item, axis)` 的默认 `magnify=1`；放大系数只作用于渲染。
- 父件编码是执行主键：补建只新增行，**绝不改写已有行的「父件编码」**。
- `tplus_bom_records` 查询必须带 `missing_since IS NULL`，复用现有 `_ACTIVE_BOM_SQL`，不重写 SQL。
- 移动端一屏免滚动是红线：PR-B 完成后须用 playwright 量页面高度取证。
- PR 描述必须记录 `Nav-Impact: updated`（两个 PR 都会动 `docs/`）。

---

# PR-A：企微表自动补建物料清单行

分支：`feat/tplus-parent-backfill-rows`

**文件结构：**

| 文件 | 责任 | 动作 |
|---|---|---|
| `services/doc-sync-worker/app/pipelines/tplus_parent_match.py` | 核对 + 补建的全部逻辑 | 修改 |
| `tests/test_tplus_parent_match.py` | 计划函数的纯函数测试 | 修改 |
| `docs/constraints/doc-sync.md` | 管道行为的事实源 | 修改 |

---

### Task 1: 补建计划函数 `plan_creates`

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/tplus_parent_match.py`
- Test: `tests/test_tplus_parent_match.py`

**Interfaces:**
- Consumes: 已有的 `cell_text(values, key)`、`text_cell(value)`、`F_PARENT_CODE`、`F_PARENT_NAME`、`F_MATCH_STATUS`、`F_CHECKED_AT`、`STATUS_OK`
- Produces: `plan_creates(records: list[dict], bom: dict[str, tuple[str, str]], checked_at: str) -> list[dict[str, Any]]`，返回可直接喂给 `client.add_records()` 的 `[{"values": {...}}, ...]`，按编码升序

- [ ] **Step 1: 写失败测试**

在 `tests/test_tplus_parent_match.py` 的 `TplusParentMatchTests` 内，`test_alert_lists_missing_rows_and_says_code_untouched` 之前插入：

```python
    def _creates(self, records, bom):
        return self.module.plan_creates(records, bom, "2026-08-06 03:00")

    def test_creates_rows_for_bom_codes_absent_from_the_sheet(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A")}}]
        creates = self._creates(records, {"A": ("甲", "v1"), "B": ("乙", "v2")})
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["values"]["父件编码"], _cells("B"))
        self.assertEqual(creates[0]["values"]["父件名称"], _cells("乙"))
        self.assertEqual(creates[0]["values"]["T+匹配状态"], _cells("一致"))
        self.assertEqual(creates[0]["values"]["T+核对时间"], _cells("2026-08-06 03:00"))

    def test_created_rows_leave_model_and_standard_columns_empty(self) -> None:
        """型号留空是人工筛选待补标准行的唯一依据，不能顺手填上。"""
        creates = self._creates([], {"B": ("乙", "v2")})
        self.assertEqual(set(creates[0]["values"]), {"父件编码", "父件名称", "T+匹配状态", "T+核对时间"})

    def test_creates_nothing_when_every_bom_code_already_has_a_row(self) -> None:
        records = [{"record_id": "r1", "values": {"父件编码": _cells("A")}}]
        self.assertEqual(self._creates(records, {"A": ("甲", "v1")}), [])

    def test_blank_code_rows_do_not_suppress_creation(self) -> None:
        """表里有一行只填了型号没填编码，不能因此认为 T+ 的编码已存在。"""
        records = [{"record_id": "r1", "values": {"型号": _cells("只有型号")}}]
        creates = self._creates(records, {"A": ("甲", "v1")})
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["values"]["父件编码"], _cells("A"))

    def test_creates_are_sorted_by_code_for_stable_batches(self) -> None:
        creates = self._creates([], {"C": ("丙", "v"), "A": ("甲", "v"), "B": ("乙", "v")})
        codes = [item["values"]["父件编码"][0]["text"] for item in creates]
        self.assertEqual(codes, ["A", "B", "C"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_tplus_parent_match -v`
Expected: 5 个新用例 FAIL，报 `module 'app.pipelines.tplus_parent_match' has no attribute 'plan_creates'`

- [ ] **Step 3: 实现 `plan_creates`**

在 `tplus_parent_match.py` 中 `plan_updates` 函数之后插入：

```python
def plan_creates(records: list[dict[str, Any]], bom: dict[str, tuple[str, str]], checked_at: str) -> list[dict[str, Any]]:
    """T+ 有、企微表没有的父件，补一行只带编码与名称的空白标准行。

    「型号」及 Lab/容差列一律留空——人工按「型号为空」筛出待补标准的行。
    编码排序是为了批次稳定，便于失败时按批重跑。
    """
    existing = {cell_text(record.get("values") or {}, F_PARENT_CODE) for record in records}
    existing.discard("")
    return [
        {"values": {
            F_PARENT_CODE: text_cell(code),
            F_PARENT_NAME: text_cell(bom[code][0]),
            F_MATCH_STATUS: text_cell(STATUS_OK),
            F_CHECKED_AT: text_cell(checked_at),
        }}
        for code in sorted(set(bom) - existing)
    ]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_tplus_parent_match -v`
Expected: 全部 PASS（含原有 9 个用例）

- [ ] **Step 5: 提交**

```bash
git add tests/test_tplus_parent_match.py services/doc-sync-worker/app/pipelines/tplus_parent_match.py
git commit -m "feat(tplus): 补建计划函数，算出企微表缺失的 T+ 父件行"
```

---

### Task 2: 核对时间改为按需写

表从 41 行涨到 T+ 全量后，现在这种"每行无条件重写核对时间"每天都要重写整表且无信息量。

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/tplus_parent_match.py:148-155`
- Test: `tests/test_tplus_parent_match.py:72-76`

**Interfaces:**
- Consumes: Task 1 无依赖，本任务独立
- Produces: `plan_updates` 的返回值中，无变化的行不再出现在 `result.updates` 里

- [ ] **Step 1: 改写既有测试为新期望**

把 `tests/test_tplus_parent_match.py` 中的 `test_unchanged_row_only_refreshes_checked_at` 整体替换为：

```python
    def test_unchanged_row_produces_no_write_at_all(self) -> None:
        """全表每天重写核对时间，在补建后会变成上千行的无效写入。"""
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("40000019"), "父件名称": _cells("已经对了"), "T+匹配状态": _cells("一致")}}]
        result = self._plan(records, {"40000019": ("已经对了", "v1")})
        self.assertEqual(result.updates, [])
        self.assertEqual(result.ok, 1)

    def test_changed_row_still_carries_the_checked_at_stamp(self) -> None:
        records = [{"record_id": "r1", "values": {
            "父件编码": _cells("40000019"), "父件名称": _cells("旧名"), "T+匹配状态": _cells("一致")}}]
        result = self._plan(records, {"40000019": ("新名", "v1")})
        self.assertEqual(result.updates[0]["values"]["T+核对时间"], _cells("2026-08-04 03:00"))
        self.assertEqual(result.updates[0]["values"]["父件名称"], _cells("新名"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_tplus_parent_match.TplusParentMatchTests.test_unchanged_row_produces_no_write_at_all -v`
Expected: FAIL，`result.updates` 实际是 `[{'record_id': 'r1', 'values': {'T+核对时间': [...]}}]` 而非 `[]`

- [ ] **Step 3: 改实现**

把 `plan_updates` 末尾的这段：

```python
        changed: dict[str, Any] = {}
        if target_name != current_name:
            changed[F_PARENT_NAME] = text_cell(target_name)
        if status != current_status:
            changed[F_MATCH_STATUS] = text_cell(status)
        # 核对时间每轮都刷新，方便一眼看出数据新鲜度。
        changed[F_CHECKED_AT] = text_cell(checked_at)
        result.updates.append({"record_id": record_id, "values": changed})
```

替换为：

```python
        changed: dict[str, Any] = {}
        if target_name != current_name:
            changed[F_PARENT_NAME] = text_cell(target_name)
        if status != current_status:
            changed[F_MATCH_STATUS] = text_cell(status)
        # 只有真的改了才盖时间戳：补建后全表上千行，每轮重写整表既无信息量又吃接口配额。
        if changed:
            changed[F_CHECKED_AT] = text_cell(checked_at)
            result.updates.append({"record_id": record_id, "values": changed})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_tplus_parent_match -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_tplus_parent_match.py services/doc-sync-worker/app/pipelines/tplus_parent_match.py
git commit -m "perf(tplus): 核对时间只在行内容变化时写，避免补建后每轮重写整表"
```

---

### Task 3: 接线补建、dry-run 保护、告警与文档

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/tplus_parent_match.py`（`MatchResult`、`build_alert`、`run_tplus_parent_match`）
- Modify: `docs/constraints/doc-sync.md:32-37`
- Test: `tests/test_tplus_parent_match.py`

**Interfaces:**
- Consumes: Task 1 的 `plan_creates(records, bom, checked_at) -> list[dict]`
- Produces: `MatchResult.created_rows: list[str]`（本轮补建的编码列表），告警文本含「🆕 补建 N 行」

- [ ] **Step 1: 写失败测试**

在 `tests/test_tplus_parent_match.py` 中，`test_alert_says_no_problem_when_everything_matches` 之后插入：

```python
    def test_alert_reports_created_rows(self) -> None:
        result = self._plan([], {})
        result.created_rows = ["A", "B"]
        text = self.module.build_alert(result)
        self.assertIn("补建 2 行", text)
        self.assertIn("A", text)
        self.assertNotIn("✅ 无异常。", text)

    def test_dry_run_never_calls_add_records(self) -> None:
        """dry-run 是确认补建量级的唯一手段，误写会直接把上千行灌进生产表。"""
        import inspect
        source = inspect.getsource(self.module.run_tplus_parent_match)
        head, _, tail = source.partition("if dry_run:")
        self.assertTrue(tail, "run_tplus_parent_match 必须保留 dry_run 分支")
        self.assertNotIn("add_records", head)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_tplus_parent_match.TplusParentMatchTests.test_alert_reports_created_rows -v`
Expected: FAIL，`MatchResult` 无 `created_rows` 属性

- [ ] **Step 3: 实现**

3a. `MatchResult` 数据类加一个字段（放在 `created_fields` 之后）：

```python
    created_rows: list[str] = field(default_factory=list)
```

3b. `build_alert` 中，在 `if result.created_fields:` 那段**之后**插入：

```python
    if result.created_rows:
        lines.append(f"🆕 按 T+ 补建 {len(result.created_rows)} 行（仅编码与名称，标准待人工补）：")
        for code in result.created_rows[:20]:
            lines.append(f"  {code}")
        if len(result.created_rows) > 20:
            lines.append(f"  …另有 {len(result.created_rows) - 20} 行")
```

并把结尾的无异常判断从：

```python
    if not result.renamed and not result.missing:
```

改为：

```python
    if not result.renamed and not result.missing and not result.created_rows:
```

3c. `run_tplus_parent_match` 中，把 `result.created_fields = created` 之后到 `if dry_run:` 之间的部分调整为——先算补建计划并计入打印，dry-run 分支要报出补建行数：

在 `result.created_fields = created` 之后插入：

```python
    creates = plan_creates(records, bom, checked_at)
    result.created_rows = [item["values"][F_PARENT_CODE][0]["text"] for item in creates]
```

把原有的 print 语句改为（末尾追加补建计数）：

```python
    print(
        f"[T+核对] 共 {result.total} 行 / 有编码 {result.with_code} / 一致 {result.ok} / "
        f"改名 {len(result.renamed)} / 失联 {len(result.missing)} / 无编码 {result.no_code} / "
        f"待补建 {len(result.created_rows)}"
    )
```

把 dry-run 分支改为：

```python
    if dry_run:
        for code in result.created_rows[:50]:
            print(f"[T+核对] 待补建 {code}｜{bom[code][0]}")
        print(f"[T+核对] dry-run，未写入（待补建 {len(result.created_rows)} 行）。")
        return 0
```

3d. 在现有的 `update_records` 批量循环**之后**、`if notify and ...` 之前插入补建写入：

```python
    for start in range(0, len(creates), 200):
        batch = creates[start:start + 200]
        response = client.add_records(docid, sheet_id, batch)
        if response.get("errcode") not in (0, None):
            print(f"[T+核对] 补建失败 errcode={response.get('errcode')} errmsg={response.get('errmsg')}")
            result.exit_code = 1
```

> `add_records` 走 `key_type` 默认值即可——`WeComSmartsheetClient.add_records` 的 payload 不含 `key_type`，字段按标题匹配与 `update_records` 一致。若实跑返回 `errcode 301031` 之类的字段定位错误，改为在 `records` 外层补 `"key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE"` 后重试。

3e. 最后把 `if notify and (...)` 的条件加上补建：

```python
    if notify and (result.missing or result.renamed or result.created_fields or result.created_rows):
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 5: 更新文档**

把 `docs/constraints/doc-sync.md` 第 32-37 行那段的末尾（`注意 tplus_bom_records 按版本累积…` 之前）插入一段：

```markdown
  2026-08-06 起同一管道还会**补建缺失行**：`tplus_bom_records` 里有、企微表没有的父件编码，
  按「父件编码 / 父件名称 / T+匹配状态=一致 / T+核对时间」四列建行，**型号与 Lab/容差列一律留空**，
  人工按「型号为空」筛出待补标准的行。补建只新增、不改写已有行的编码。
  **人工删掉的行下一轮会被重新建出来**（已确认接受，无删除白名单）。
  同时核对时间改为只在该行内容变化时才写——全量后每轮重写整表既无信息量又吃接口配额。
  首次上线务必先 `--dry-run` 看待补建行数再实跑。
```

- [ ] **Step 6: 提交并开 PR**

```bash
git add tests/test_tplus_parent_match.py services/doc-sync-worker/app/pipelines/tplus_parent_match.py docs/constraints/doc-sync.md
git commit -m "feat(tplus): 按 T+ 有效父件补建企微标准型号表缺失行"
git push -u origin feat/tplus-parent-backfill-rows
gh pr create --title "feat(tplus): 按 T+ 有效父件补建企微标准型号表缺失行" --body "$(cat <<'EOF'
## 改动
- `plan_creates()`：算出 T+ 有、企微表没有的父件，补建只带编码与名称的空白标准行
- 核对时间改为按需写，避免补建后每轮重写整表
- 告警与 dry-run 输出补建行数

## 行为边界
- 补建只新增，绝不改写已有行的「父件编码」
- 人工删掉的行下一轮会被重新建出来（已确认接受）
- 型号与 Lab/容差列留空，人工按「型号为空」筛待补标准行

## 验证
`python -m unittest discover -s tests` 全绿。生产先 `python -m app.main tplus-parent-match --dry-run` 看待补建行数，确认量级后再实跑。

Nav-Impact: updated
EOF
)"
```

- [ ] **Step 7: 生产 dry-run（合并后，需用户在场确认量级）**

在 txecs 的 `business-cn-doc-sync-worker-1` 容器内：

```bash
python -m app.main tplus-parent-match --dry-run
```

Expected: 打印 `待补建 N 行`。**把 N 报给用户确认后**再去掉 `--dry-run` 实跑一次，然后到企微表核对新行的四列取值与空列。

---

### Task 4: BOM 同步完成后事件触发补建

用户 2026-08-06 拍板：补建触发 = **每日兜底（Task 3 已有）+ BOM 同步完成后事件触发**。物料清单共 200 多条，首次补齐后只有 BOM 变动才增量补。

**链路事实（已核实，不要重新试探）：**
- T+ BOM 同步记录写在 `integration_sync_runs`，唯一写入点是 `services/tplus-sync-worker/src/tplus_datahub/jobs/db_sync_requests.py:118 finish_bom_request()`，其中 `module` 硬编码为 `'bom'`、`provider` 硬编码为 `'chanjet'`，定时全量与增量请求都走这里（只有 `mode` 不同）。`sync_state.py:133` 那个 `record_tplus_sync_run_if_configured` 没有任何调用点，是死代码，不要依赖它。
- doc-sync-worker 的 `worker_loop.py` 每 `DOC_SYNC_POLL_SECONDS`（默认 30s）跑一次 `_default_consume_requests()`，这是事件触发的落点。

**为什么用拉取式而非跨服务写入：** doc-sync-worker 在自己的 poll 周期里查 BOM 同步水位，涨了就跑一次核对+补建。tplus-sync-worker **完全不用改**，不新增表、不新增配置项，水位存在 worker 进程内存里即可——补建是幂等的（`plan_creates` 对已存在的编码返回空），重启多跑一次无害。

**Files:**
- Modify: `services/doc-sync-worker/app/pipelines/tplus_parent_match.py`
- Modify: `services/doc-sync-worker/app/pipelines/worker_loop.py:112-121`
- Test: `tests/test_tplus_parent_match.py`
- Modify: `docs/constraints/doc-sync.md`

**Interfaces:**
- Consumes: Task 3 的 `run_tplus_parent_match(*, dry_run=False, notify=True) -> int`
- Produces: `latest_bom_sync_at() -> datetime | None`、`run_backfill_if_bom_synced(last_seen: datetime | None) -> tuple[datetime | None, bool]`（返回 `(新水位, 是否真的跑了)`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_tplus_parent_match.py` 的 `TplusParentMatchTests` 类内追加：

```python
    def test_bom_watermark_sql_matches_the_only_real_writer(self) -> None:
        """integration_sync_runs 里 BOM 记录的 provider/module 是硬编码的，写错就永远触发不了。"""
        sql = self.module._LATEST_BOM_SYNC_SQL
        self.assertIn("integration_sync_runs", sql)
        self.assertIn("provider = 'chanjet'", sql)
        self.assertIn("module = 'bom'", sql)
        # 故意不过滤 status：取值猜错会导致水位永不上涨，而补建是幂等的，宁可多跑一次。
        self.assertNotIn("status", sql)

    def test_first_poll_only_records_the_watermark(self) -> None:
        """首轮不跑：容器重启风暴不该反复触发补建，当天的兜底轮已覆盖。"""
        from datetime import datetime, timezone
        stamp = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)
        calls = []
        self.module.latest_bom_sync_at = lambda: stamp
        self.module.run_tplus_parent_match = lambda **kwargs: calls.append(kwargs) or 0
        watermark, ran = self.module.run_backfill_if_bom_synced(None)
        self.assertEqual(watermark, stamp)
        self.assertFalse(ran)
        self.assertEqual(calls, [])

    def test_rising_watermark_triggers_one_backfill(self) -> None:
        from datetime import datetime, timedelta, timezone
        old = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)
        new = old + timedelta(minutes=5)
        calls = []
        self.module.latest_bom_sync_at = lambda: new
        self.module.run_tplus_parent_match = lambda **kwargs: calls.append(kwargs) or 0
        watermark, ran = self.module.run_backfill_if_bom_synced(old)
        self.assertEqual(watermark, new)
        self.assertTrue(ran)
        self.assertEqual(len(calls), 1)

    def test_flat_watermark_does_not_retrigger(self) -> None:
        from datetime import datetime, timezone
        stamp = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)
        calls = []
        self.module.latest_bom_sync_at = lambda: stamp
        self.module.run_tplus_parent_match = lambda **kwargs: calls.append(kwargs) or 0
        watermark, ran = self.module.run_backfill_if_bom_synced(stamp)
        self.assertEqual(watermark, stamp)
        self.assertFalse(ran)
        self.assertEqual(calls, [])

    def test_unreadable_watermark_keeps_the_old_one_and_does_not_run(self) -> None:
        """DB 读不到时保持原水位：清成 None 会让下一轮把首轮逻辑再走一遍，白跑一次。"""
        from datetime import datetime, timezone
        stamp = datetime(2026, 8, 6, 3, 0, tzinfo=timezone.utc)
        calls = []
        self.module.latest_bom_sync_at = lambda: None
        self.module.run_tplus_parent_match = lambda **kwargs: calls.append(kwargs) or 0
        watermark, ran = self.module.run_backfill_if_bom_synced(stamp)
        self.assertEqual(watermark, stamp)
        self.assertFalse(ran)
        self.assertEqual(calls, [])
```

> 这些用例直接改写模块属性，`setUp` 里每次都重新 import 模块（已有的 `_clear_app_modules()` 机制），不会串味。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_tplus_parent_match -v`
Expected: 5 个新用例 FAIL，报 `module ... has no attribute '_LATEST_BOM_SYNC_SQL'` / `'run_backfill_if_bom_synced'`

- [ ] **Step 3a: 实现水位查询与触发判断**

在 `tplus_parent_match.py` 中 `run_tplus_parent_match` 之后追加：

```python
# BOM 同步记录的唯一真实写入点是 tplus-sync-worker 的 finish_bom_request()，
# 那里 provider 与 module 都是硬编码；写错这两个值会让事件触发永远不生效。
# 故意不过滤 status：取值猜错的代价是"永不触发"，而补建幂等，多跑一次无害。
_LATEST_BOM_SYNC_SQL = """
SELECT MAX(finished_at)
FROM integration_sync_runs
WHERE provider = 'chanjet' AND module = 'bom'
"""


def latest_bom_sync_at() -> datetime | None:
    """T+ BOM 最近一次同步的完成时间；读不到一律返回 None（不抛，不拖垮轮询）。"""
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_LATEST_BOM_SYNC_SQL)
                row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception as exc:  # noqa: BLE001 - 水位读不到只是本轮不触发
        print(f"[T+核对] 读取 BOM 同步水位失败：{exc}")
        return None


def run_backfill_if_bom_synced(last_seen: datetime | None) -> tuple[datetime | None, bool]:
    """BOM 同步水位涨了就跑一次核对+补建，返回 (新水位, 是否真的跑了)。

    首轮（last_seen 为 None）只记水位不跑：容器重启风暴不该反复触发，当天的兜底轮已覆盖。
    读不到水位时保持原值——清成 None 会让下一轮把首轮逻辑再走一遍，白跑一次。
    """
    current = latest_bom_sync_at()
    if current is None:
        return last_seen, False
    if last_seen is None or current <= last_seen:
        return current, False
    run_tplus_parent_match()
    return current, True
```

并在文件顶部的 import 区补上 `from app.storage.postgres import connect`（若已存在则不重复添加）。`datetime` 已在顶部导入。

- [ ] **Step 3b: 接进 worker_loop 的轮询周期**

修改 `services/doc-sync-worker/app/pipelines/worker_loop.py`：

把 import 行 `from app.pipelines.tplus_parent_match import run_tplus_parent_match` 改为：

```python
from app.pipelines.tplus_parent_match import run_backfill_if_bom_synced, run_tplus_parent_match
```

在 `run_worker_loop` 内、`def _default_full_sync()` 之前加一行闭包状态：

```python
    # BOM 同步水位存进程内存即可：补建幂等，重启后首轮只记水位不跑。
    bom_watermark: datetime | None = None
```

把 `_default_consume_requests()` 改为：

```python
    def _default_consume_requests() -> int:
        nonlocal bom_watermark
        code = run_pending_sync_requests(limit=10)
        backup_code = run_pending_structure_backup_jobs(limit=10)
        try:
            written = run_write_rnd_records()
            if written:
                print(f"[文档同步循环] 研发过程记录写入 {written} 条。")
        except Exception as exc:  # noqa: BLE001 - 写表失败不拖垮轮询
            print(f"[文档同步循环] 研发过程记录写入异常：{exc}")
        # T+ BOM 同步完成后立刻补建，不必等次日兜底轮。
        try:
            bom_watermark, ran = run_backfill_if_bom_synced(bom_watermark)
            if ran:
                print("[文档同步循环] 检测到 T+ BOM 新同步，已跑一次父件核对与补建。")
        except Exception as exc:  # noqa: BLE001 - 核对失败不拖垮轮询
            print(f"[文档同步循环] T+ 事件触发核对异常：{exc}")
        return code or backup_code
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 5: 更新文档**

在 `docs/constraints/doc-sync.md` 中 Task 3 加的那段之后追加：

```markdown
  触发时机是**每日兜底 + 事件触发**双通道：兜底走 `run-loop` 的全量周期；事件触发在同一 loop 的
  poll 周期（`DOC_SYNC_POLL_SECONDS`，默认 30s）里查 `integration_sync_runs` 中
  `provider='chanjet' AND module='bom'` 的 `MAX(finished_at)`，水位上涨即跑一次核对与补建。
  水位存 worker 进程内存，重启后首轮只记水位不跑（补建幂等，兜底轮已覆盖）。
  该 SQL 的 provider/module 对应 tplus-sync-worker `db_sync_requests.py` 的 `finish_bom_request()`
  硬编码值，**改那边就要同步改这里**，否则事件触发会静默失效。
```

- [ ] **Step 6: 提交**

```bash
git add tests/test_tplus_parent_match.py services/doc-sync-worker/app/pipelines/tplus_parent_match.py services/doc-sync-worker/app/pipelines/worker_loop.py docs/constraints/doc-sync.md
git commit -m "feat(tplus): T+ BOM 同步完成后即时触发父件核对与补建"
```

---

# PR-B：标签层与设置重组

分支：`feat/colors-label-layer-and-settings`

**文件结构：**

| 文件 | 责任 | 动作 |
|---|---|---|
| `services/public-web/formula/colors/index.html` | 页面全部内容（样式 + DOM + 内联 ES module） | 修改 |
| `tests/test_formula_color_space_frontend.py` | 对 HTML 源码的字符串断言 | 修改 |
| `docs/superpowers/specs/2026-08-06-formula-colors-sync-and-view-design.md` | 设计依据 | 已随 PR-A 入库 |

> 该页面是单文件内联脚本，657 行。所有改动都在这一个文件里，**元素 id 一律沿用**，只改 DOM 位置与容器，避免大面积改写 `$('...')` 调用。

---

### Task 4: 色点标签换成 DOM 覆盖层

先做等价替换：行为仍是"只有选中点有标签"，但底层从 canvas Sprite 换成 DOM，并接上距离淡化。高亮集这一步先返回 `null`（Task 8 填实现）。

**Files:**
- Modify: `services/public-web/formula/colors/index.html`
- Test: `tests/test_formula_color_space_frontend.py:179-185`

**Interfaces:**
- Consumes: 已有的 `positionFor(item)`、`labelValue(item, field)`、`state.labelFields`、`state.selected`、`state.hoveredId`、`controls`
- Produces: `rebuildLabels()`（数据/勾选变化时重建 DOM）、`syncLabels()`（每帧写 transform/opacity）、`labelHighlightSet()`（返回 `Set<number> | null`）、`state.labelFocal`（数字，淡化距离）

- [ ] **Step 1: 改写既有测试为新期望**

把 `tests/test_formula_color_space_frontend.py` 的 `test_detail_fields_control_selected_product_3d_label` 整体替换为：

```python
    def test_detail_fields_drive_a_dom_label_layer(self) -> None:
        self.assertGreaterEqual(self.html.count('data-label-field="'), 11)
        self.assertIn("labelFields:new Set(['formula','resin','dosage'])", self.html)
        self.assertIn("function rebuildLabels", self.html)
        self.assertIn("function syncLabels", self.html)
        self.assertIn("checkbox.onchange", self.html)
        # 标签改成 DOM 层：canvas sprite 每块要建 192×72 纹理，全量显示时会吃爆显存。
        self.assertIn('class="label-layer"', self.html)
        self.assertIn("el.className='point-label'", self.html)
        self.assertNotIn("selectedLabelCanvas", self.html)
        self.assertNotIn("function updateSelectedLabel", self.html)

    def test_label_opacity_uses_cosine_fade_from_the_orbit_target(self) -> None:
        # 余弦缓动：焦点附近与远端都平缓，过渡集中在中段，比线性自然。
        self.assertIn("controls.getTarget(_focalV3)", self.html)
        self.assertIn(".5-.5*Math.cos(normalized*Math.PI)", self.html)
        self.assertIn("function labelOpacityBase", self.html)
        self.assertIn("function labelHighlightSet", self.html)
        # 三态：hover/选中全亮，非高亮压到 20% 以下，其余按距离淡化。
        self.assertIn("Math.min(base,.2)", self.html)

    def test_label_layer_only_repaints_when_the_camera_moved(self) -> None:
        # camera-controls 的 update() 返回是否有相机变化，是最可靠的节流信号。
        self.assertIn("const cameraMoved=controls.update(clock.getDelta())", self.html)
        self.assertIn("if(cameraMoved||labelsDirty)", self.html)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_formula_color_space_frontend -v`
Expected: 3 个用例 FAIL（`function rebuildLabels` 等不在 HTML 中）

- [ ] **Step 3a: 加样式**

在 `<style>` 内 `.tooltip{...}` 规则之后（同一行末尾）追加：

```css
.label-layer{position:absolute;inset:0;overflow:hidden;pointer-events:none}.point-label{position:absolute;left:0;top:0;padding:2px 6px;border-radius:6px;background:rgba(24,20,16,.62);color:#fff;font-size:12px;line-height:1.35;white-space:pre;transform-origin:0 0;will-change:transform,opacity}
```

- [ ] **Step 3b: 加 DOM 容器**

把 `<div id="tooltip" class="tooltip"></div>` 改为：

```html
      <div id="labelLayer" class="label-layer"></div>
      <div id="tooltip" class="tooltip"></div>
```

- [ ] **Step 3c: 删掉 Sprite 标签实现**

删除这三处：

1. `const selectedLabelCanvas=...;scene.add(selectedLabel);` 整行（原第 268 行）
2. `function positionSelectedLabel(){...}` 整个函数（原第 298 行）
3. `function updateSelectedLabel(){...}` 整个函数（原第 299-301 行）

并把这些调用点一并处理：
- `renderScene()` 内的 `positionSelectedLabel();` → `syncLabels();`
- `selectPoint()` 末尾的 `updateSelectedLabel();` → `rebuildLabels();`
- `resetSelection()` 内的 `selectedLabel.visible=false;` → `rebuildLabels();`
- `document.querySelectorAll('[data-label-field]')` 那行的 `updateSelectedLabel()` → `rebuildLabels()`

- [ ] **Step 3d: 实现 DOM 标签层**

在 `function labelValue(...)` 之后插入：

```js
  // 标签用 DOM 而非 canvas sprite：全量显示时几百块 192×72 纹理会吃爆显存，
  // DOM 层几乎零成本、文字锐利，投影计算与 hitAt() 用的是同一套。
  const labelLayer=$('labelLayer'),labelEntries=[],_labelV3=new THREE.Vector3(),_focalV3=new THREE.Vector3();
  let labelsDirty=true;
  // 余弦缓动淡化：焦点取 camera-controls 的 orbit target，
  // focusSelected / fitToBox 之后它正好是用户的关注点。
  function labelOpacityBase(worldPos){
    controls.getTarget(_focalV3);
    const normalized=Math.min(worldPos.distanceTo(_focalV3),state.labelFocal)/state.labelFocal;
    return 1-(.5-.5*Math.cos(normalized*Math.PI));
  }
  // Task 8 会把重叠型号填进来；返回 null 表示「无高亮集」，此时不压暗任何点。
  function labelHighlightSet(){return null;}
  function labelTargets(){
    if(!state.labelFields.size)return[];
    if(state.showAllLabels)return state.points;
    return state.selected?state.points.filter((item)=>item.id===state.selected.id):[];
  }
  function rebuildLabels(){
    labelLayer.textContent='';labelEntries.length=0;
    for(const item of labelTargets()){
      const text=[...state.labelFields].map((field)=>labelValue(item,field)).filter(Boolean).join('\n');
      if(!text)continue;
      const el=document.createElement('div');el.className='point-label';el.textContent=text;
      labelLayer.appendChild(el);labelEntries.push({item,el});
    }
    labelsDirty=true;
  }
  function syncLabels(){
    if(!labelEntries.length)return;
    const width=labelLayer.clientWidth,height=labelLayer.clientHeight,highlight=labelHighlightSet();
    for(const {item,el} of labelEntries){
      const worldPos=positionFor(item);
      _labelV3.copy(worldPos).project(camera);
      // z>1 是相机背后，投影坐标会翻折到画面里。
      if(_labelV3.z>1){el.style.display='none';continue;}
      el.style.display='';
      el.style.transform=`translate(${(_labelV3.x+1)/2*width}px,${(1-_labelV3.y)/2*height}px)`;
      const base=labelOpacityBase(worldPos),focused=state.hoveredId===item.id||(state.selected&&state.selected.id===item.id);
      el.style.opacity=(focused?1:highlight&&!highlight.has(item.id)?Math.min(base,.2):base).toFixed(3);
    }
  }
```

- [ ] **Step 3e: 加 state 字段**

在 `const state={...}` 中，把 `labelFields:new Set(['formula','resin','dosage'])` 改为：

```js
labelFields:new Set(['formula','resin','dosage']),showAllLabels:false,labelFocal:12
```

- [ ] **Step 3f: 接进渲染循环**

把 `renderScene()` 末尾（`updateReferenceReadout();` 之前）加上 `rebuildLabels();`，并把 `animate()` 改为：

```js
  function animate(){requestAnimationFrame(animate);const cameraMoved=controls.update(clock.getDelta());syncSceneScale();if(cameraMoved||labelsDirty){syncLabels();labelsDirty=false;}renderer.render(scene,camera);}animate();
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 5: 浏览器 smoke**

启动本地：`docker compose -f local/docker-compose.local.yml up -d public-web`，浏览器打开页面，切到「参考示例」（不需登录），点任意色点，确认：标签出现在点旁边、转动相机时标签跟随、拉远时标签变淡。

- [ ] **Step 6: 提交**

```bash
git add services/public-web/formula/colors/index.html tests/test_formula_color_space_frontend.py
git commit -m "refactor(colors): 色点标签换成 DOM 层并按距离余弦淡化"
```

---

### Task 5: 全部显示标签开关 + 淡化距离滑块

**Files:**
- Modify: `services/public-web/formula/colors/index.html`
- Test: `tests/test_formula_color_space_frontend.py`

**Interfaces:**
- Consumes: Task 4 的 `rebuildLabels()`、`state.showAllLabels`、`state.labelFocal`、`labelsDirty`
- Produces: DOM 元素 `#toggleAllLabels`、`#labelFocal`、`#labelFocalValue`

- [ ] **Step 1: 写失败测试**

在 `tests/test_formula_color_space_frontend.py` 中，`test_label_layer_only_repaints_when_the_camera_moved` 之后插入：

```python
    def test_all_labels_can_be_shown_at_once(self) -> None:
        self.assertIn('id="toggleAllLabels"', self.html)
        self.assertIn("showAllLabels:false", self.html)
        self.assertIn("state.showAllLabels=", self.html)
        # 全量显示时不再限制点数：DOM 标签没有纹理开销。
        self.assertIn("if(state.showAllLabels)return state.points", self.html)

    def test_label_fade_distance_is_adjustable(self) -> None:
        self.assertIn('id="labelFocal" type="range" min="2" max="60"', self.html)
        self.assertIn("labelFocal:12", self.html)
        self.assertIn('id="labelFocalValue"', self.html)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_formula_color_space_frontend.FormulaColorSpaceFrontendTests.test_all_labels_can_be_shown_at_once -v`
Expected: FAIL，`id="toggleAllLabels"` 不在 HTML 中

- [ ] **Step 3a: 加控件**

在 `<details id="viewSettings">` 的 `view-settings-body` 内，最后一个 `</div>`（参考色域那组）之后插入：

```html
          <div class="reference-controls" aria-label="标签显示设置">
            <label class="slice-switch"><input id="toggleAllLabels" type="checkbox"/>全部显示标签</label>
            <label>淡化距离 <span id="labelFocalValue">12</span><input id="labelFocal" type="range" min="2" max="60" step="1" value="12"/></label>
          </div>
```

> Task 6 会把这段连同整个 `view-settings-body` 搬到顶部面板，此处先就地加以保证本任务可独立验收。

- [ ] **Step 3b: 接事件**

在 `$('toggleOverlapOnly').onchange=...` 那行之后插入：

```js
  $('toggleAllLabels').onchange=(event)=>{state.showAllLabels=event.target.checked;rebuildLabels();};
  $('labelFocal').oninput=(event)=>{state.labelFocal=Number(event.target.value);$('labelFocalValue').textContent=event.target.value;labelsDirty=true;};
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 5: 浏览器 smoke**

勾「全部显示标签」，确认所有色点都出现标签；勾/取消型号详情里的字段，确认**所有**标签同步变化；拖淡化距离滑块，确认远处标签的淡出程度随之变化。

- [ ] **Step 6: 提交**

```bash
git add services/public-web/formula/colors/index.html tests/test_formula_color_space_frontend.py
git commit -m "feat(colors): 支持全部色点显示标签与淡化距离调节"
```

---

### Task 6: 设置收纳到顶部面板

**Files:**
- Modify: `services/public-web/formula/colors/index.html`
- Test: `tests/test_formula_color_space_frontend.py:87-95`

**Interfaces:**
- Consumes: Task 5 的 `#toggleAllLabels`、`#labelFocal`
- Produces: `#toggleSettings`（齿轮按钮）、`#settingsPanel`（横幅面板）。所有被搬运的控件 id 保持不变

- [ ] **Step 1: 改写既有测试为新期望**

把 `tests/test_formula_color_space_frontend.py` 的 `test_view_controls_are_collapsible` 整体替换为：

```python
    def test_view_controls_live_in_a_top_panel_not_over_the_canvas(self) -> None:
        # 浮层压在画布上会挡住三维图；改为顶部横幅面板，展开时挤压画布高度。
        self.assertNotIn('<details id="viewSettings"', self.html)
        self.assertNotIn("<summary>视图设置</summary>", self.html)
        self.assertIn('id="toggleSettings"', self.html)
        self.assertIn('id="settingsPanel"', self.html)
        self.assertIn("显示设置", self.html)
        for control in ("toggleReference", "resetCamera", "topCamera", "frontCamera",
                        "sideCamera", "focusSelected", "togglePan", "toggleTolerance",
                        "toleranceMagnify", "referenceMode", "toggleAllLabels", "labelFocal"):
            self.assertIn(f'id="{control}"', self.html)

    def test_settings_panel_sits_between_toolbar_and_workspace(self) -> None:
        toolbar = self.html.index('class="toolbar"')
        panel = self.html.index('id="settingsPanel"')
        workspace = self.html.index('class="workspace"')
        self.assertLess(toolbar, panel)
        self.assertLess(panel, workspace)

    def test_settings_panel_groups_are_labelled(self) -> None:
        for group in ("观察视角", "显示开关", "容差盒", "参考色域", "标签"):
            self.assertIn(group, self.html)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_formula_color_space_frontend -v`
Expected: 3 个用例 FAIL（`<details id="viewSettings"` 仍在）

- [ ] **Step 3a: 顶部按钮**

把 `.mode-row` 那一行改为：

```html
    <div class="mode-row" aria-label="数据源与视图"><button id="datasetLive" class="active">标准型号</button><button id="datasetMock">参考示例</button><button id="deltaView">Δ 判色视图</button><button id="toggleSettings">⚙ 显示设置</button></div>
```

- [ ] **Step 3b: 面板 DOM**

删除整个 `<details id="viewSettings" class="view-settings">…</details>`（含内部全部控件），把其中的控件按分组重排进新的 `<section>`，插在 `</section>`（toolbar 结束）与 `<main class="workspace">` 之间：

```html
  <section id="settingsPanel" class="settings-panel" hidden>
    <div class="settings-group" aria-label="观察视角"><h4>观察视角</h4><div class="stage-actions"><button id="resetCamera">复位</button><button id="topCamera">a*b* 俯视</button><button id="frontCamera">L*–a* 立面</button><button id="sideCamera">L*–b* 立面</button><button id="focusSelected">聚焦选中</button><button id="togglePan">单指平移</button></div></div>
    <div class="settings-group" aria-label="显示开关"><h4>显示开关</h4><div class="stage-actions"><button id="toggleTolerance" class="active">隐藏容差盒</button><button id="toggleProducts" class="active">隐藏标准色点</button><button id="toggleReference">显示参考色域</button></div></div>
    <div class="settings-group" aria-label="容差盒"><h4>容差盒</h4><div class="reference-controls">
      <label>容差放大<select id="toleranceMagnify"><option value="1">×1 真实比例</option><option value="5">×5</option><option value="10">×10</option><option value="20">×20</option><option value="50">×50</option></select></label>
      <label>透明度 <span id="toleranceOpacityValue">30%</span><input id="toleranceOpacity" type="range" min="8" max="90" value="30"/></label>
      <label class="slice-switch"><input id="toggleToleranceEdges" type="checkbox" checked/>显示盒棱线</label>
      <label class="slice-switch"><input id="toggleOverlapOnly" type="checkbox"/>只看容差重叠</label>
    </div></div>
    <div class="settings-group" aria-label="参考色域"><h4>参考色域</h4><div class="reference-controls">
      <label>色域<select id="referenceMode"><option value="overlay">sRGB + Display-P3</option><option value="srgb">仅 sRGB</option><option value="p3">仅 Display-P3</option><option value="difference">P3 − sRGB 差集</option></select></label>
      <label>显示方式<select id="referenceStyle"><option value="surface">方块外壳</option><option value="solid">方块实体</option><option value="wireframe">线框方块</option></select></label>
      <label>透明度 <span id="referenceOpacityValue">32%</span><input id="referenceOpacity" type="range" min="10" max="100" value="32"/></label>
      <label class="slice-switch"><input id="toggleLSlice" type="checkbox"/>L* 水平切面</label>
      <label>切面 L* <span id="sliceLValue">50</span><input id="sliceL" type="range" min="0" max="100" step="1" value="50" disabled/></label>
      <span id="deviceGamut" class="device-gamut"></span>
    </div></div>
    <div class="settings-group" aria-label="标签"><h4>标签</h4><div class="reference-controls">
      <label class="slice-switch"><input id="toggleAllLabels" type="checkbox"/>全部显示标签</label>
      <label>淡化距离 <span id="labelFocalValue">12</span><input id="labelFocal" type="range" min="2" max="60" step="1" value="12"/></label>
      <button id="saveViewPrefs">设为默认</button><button id="resetViewPrefs">恢复默认</button>
    </div></div>
  </section>
```

> `#saveViewPrefs` / `#resetViewPrefs` 在 Task 7 接事件，此处先放进 DOM，避免再动一次面板结构。

- [ ] **Step 3c: 样式**

删除 `.view-settings`、`.view-settings summary`、`.view-settings[open] summary`、`.view-settings-body` 四条规则（以及 `@media` 里的 `.view-settings{right:8px;left:8px;bottom:8px}`），加入：

```css
.settings-panel{display:flex;gap:14px;flex-wrap:wrap;padding:10px 14px;background:rgba(255,253,249,.92);border-bottom:1px solid var(--line)}.settings-panel[hidden]{display:none}.settings-group{display:grid;gap:6px;align-content:start}.settings-group h4{margin:0;color:var(--muted);font-size:11px;font-weight:750;letter-spacing:.04em}
```

并在 `@media(max-width:620px)` 内追加：

```css
.settings-panel{gap:9px;padding:8px}.settings-group{width:100%}
```

- [ ] **Step 3d: 接开关事件**

在 `$('deltaView').onclick=...` 那行之后插入：

```js
  // 面板是 .page 这个 flex column 的一个节点，展开时 .workspace 的 flex:1 自动让出高度，
  // 不覆盖三维画布。ResizeObserver 已监听 #stage，相机 aspect 会自动跟上。
  $('toggleSettings').onclick=()=>{const panel=$('settingsPanel');panel.hidden=!panel.hidden;$('toggleSettings').classList.toggle('active',!panel.hidden);};
```

- [ ] **Step 3e: 删掉移动端收起浮层的那行**

删除：

```js
  if(matchMedia('(max-width:1000px)').matches)$('viewSettings').removeAttribute('open');
```

（面板本身默认 `hidden`，无需再判断。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 5: 浏览器 smoke**

点「⚙ 显示设置」展开/收起，确认：面板出现在工具栏下方、画布高度相应收缩而非被遮挡、面板内每个控件仍然生效（改容差透明度、切参考色域模式、点复位视角各试一次）。

- [ ] **Step 6: 提交**

```bash
git add services/public-web/formula/colors/index.html tests/test_formula_color_space_frontend.py
git commit -m "refactor(colors): 视图设置从画布浮层收进顶部面板"
```

---

### Task 7: 设为默认（localStorage）

**Files:**
- Modify: `services/public-web/formula/colors/index.html`
- Test: `tests/test_formula_color_space_frontend.py`

**Interfaces:**
- Consumes: Task 6 的 `#saveViewPrefs`、`#resetViewPrefs`；Task 4 的 `state.labelFields`、`state.showAllLabels`、`state.labelFocal`
- Produces: `savePrefs()`、`loadPrefs()`、`applyPrefsToControls()`

- [ ] **Step 1: 写失败测试**

在 `tests/test_formula_color_space_frontend.py` 中，`test_settings_panel_groups_are_labelled` 之后插入：

```python
    def test_label_preferences_can_be_saved_as_default(self) -> None:
        self.assertIn("aliecs_formula_colors_view_prefs", self.html)
        self.assertIn('id="saveViewPrefs"', self.html)
        self.assertIn('id="resetViewPrefs"', self.html)
        self.assertIn("function savePrefs", self.html)
        self.assertIn("function loadPrefs", self.html)
        self.assertIn("function applyPrefsToControls", self.html)

    def test_only_label_preferences_persist(self) -> None:
        """容差盒/参考色域等不进 localStorage，否则下次打开会莫名其妙是隐藏状态。"""
        prefs = self.html[self.html.index("function savePrefs"):self.html.index("function loadPrefs")]
        self.assertIn("labelFields", prefs)
        self.assertIn("showAllLabels", prefs)
        self.assertIn("labelFocal", prefs)
        for leaked in ("showTolerance", "showReference", "toleranceMagnify", "referenceMode"):
            self.assertNotIn(leaked, prefs)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_formula_color_space_frontend.FormulaColorSpaceFrontendTests.test_label_preferences_can_be_saved_as_default -v`
Expected: FAIL，`aliecs_formula_colors_view_prefs` 不在 HTML 中

- [ ] **Step 3a: 实现**

在 `$('loginBtn').onclick=...` 那行之后插入：

```js
  // 只持久化标签相关的三项。容差盒/参考色域等不存，
  // 否则下次打开时容差盒莫名其妙是隐藏的，排查成本远高于省下的两次点击。
  const PREFS_KEY='aliecs_formula_colors_view_prefs';
  function savePrefs(){
    localStorage.setItem(PREFS_KEY,JSON.stringify({labelFields:[...state.labelFields],showAllLabels:state.showAllLabels,labelFocal:state.labelFocal}));
    $('saveViewPrefs').textContent='已设为默认';setTimeout(()=>{$('saveViewPrefs').textContent='设为默认';},1600);
  }
  function loadPrefs(){
    let prefs=null;try{prefs=JSON.parse(localStorage.getItem(PREFS_KEY)||'null');}catch{prefs=null;}
    if(!prefs)return;
    if(Array.isArray(prefs.labelFields))state.labelFields=new Set(prefs.labelFields);
    if(typeof prefs.showAllLabels==='boolean')state.showAllLabels=prefs.showAllLabels;
    if(Number.isFinite(prefs.labelFocal))state.labelFocal=prefs.labelFocal;
  }
  function applyPrefsToControls(){
    document.querySelectorAll('[data-label-field]').forEach((checkbox)=>{checkbox.checked=state.labelFields.has(checkbox.dataset.labelField);});
    $('toggleAllLabels').checked=state.showAllLabels;
    $('labelFocal').value=String(state.labelFocal);$('labelFocalValue').textContent=String(state.labelFocal);
  }
  $('saveViewPrefs').onclick=savePrefs;
  $('resetViewPrefs').onclick=()=>{
    localStorage.removeItem(PREFS_KEY);
    state.labelFields=new Set(['formula','resin','dosage']);state.showAllLabels=false;state.labelFocal=12;
    applyPrefsToControls();rebuildLabels();
  };
```

- [ ] **Step 3b: 启动时套用**

把页面末尾的启动序列：

```js
  setPanMode(false);rebuildReferenceGamut();resetCamera(false);
  switchDataset('live');
```

改为：

```js
  loadPrefs();applyPrefsToControls();
  setPanMode(false);rebuildReferenceGamut();resetCamera(false);
  switchDataset('live');
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 5: 浏览器 smoke**

改几个勾选 + 打开「全部显示标签」 + 调淡化距离 → 点「设为默认」 → **刷新页面** → 确认三项都被还原。再点「恢复默认」 → 刷新 → 确认回到出厂状态（型号/基料/比例三项勾选、不全量显示、距离 12）。

- [ ] **Step 6: 提交**

```bash
git add services/public-web/formula/colors/index.html tests/test_formula_color_space_frontend.py
git commit -m "feat(colors): 标签勾选与淡化设置可存为默认"
```

---

### Task 8: hover 联动高亮容差重叠型号

**Files:**
- Modify: `services/public-web/formula/colors/index.html`
- Test: `tests/test_formula_color_space_frontend.py`

**Interfaces:**
- Consumes: 已有的 `boxesOverlap(a, b)`、`state.hoveredId`、`state.selected`、`state.points`；Task 4 的 `labelHighlightSet()` 占位
- Produces: `labelHighlightSet()` 的真实实现，返回 `Set<number> | null`；`rebuildTolerance()` 内容差盒 opacity 随高亮集衰减

- [ ] **Step 1: 写失败测试**

在 `tests/test_formula_color_space_frontend.py` 中，`test_only_label_preferences_persist` 之后插入：

```python
    def test_hover_highlights_tolerance_overlapping_models(self) -> None:
        self.assertIn("function labelHighlightSet", self.html)
        self.assertIn("boxesOverlap(focus,item)", self.html)
        # 重叠判定必须走真实比例，放大后的盒会得出错误结论。
        self.assertIn("function boxesOverlap", self.html)
        self.assertIn("toleranceRange(a,axis)", self.html)
        # 孤立型号没有重叠邻居，此时不该把全场压暗。
        self.assertIn("return set.size>1?set:null", self.html)

    def test_highlight_dims_unrelated_tolerance_boxes_too(self) -> None:
        self.assertIn("const highlight=labelHighlightSet()", self.html)
        self.assertIn("dimmed=highlight&&!highlight.has(item.id)", self.html)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_formula_color_space_frontend.FormulaColorSpaceFrontendTests.test_hover_highlights_tolerance_overlapping_models -v`
Expected: FAIL，`boxesOverlap(focus,item)` 不在 HTML 中

- [ ] **Step 3a: 实现高亮集**

把 Task 4 留下的占位：

```js
  function labelHighlightSet(){return null;}
```

替换为：

```js
  // hover 或选中某型号时，高亮与它容差盒重叠的型号、压暗其余——
  // 比「只看容差重叠」那个二元筛选多一条不改筛选就能看清重叠关系的路径。
  // 没有重叠邻居就返回 null：孤立型号不该把全场压暗。
  function labelHighlightSet(){
    const focus=state.hoveredId!==null?state.points.find((item)=>item.id===state.hoveredId):state.selected;
    if(!focus||!focus.tolerance)return null;
    const set=new Set([focus.id]);
    for(const item of state.points)if(item.id!==focus.id&&item.tolerance&&boxesOverlap(focus,item))set.add(item.id);
    return set.size>1?set:null;
  }
```

- [ ] **Step 3b: 容差盒同步压暗**

在 `rebuildTolerance()` 的 `for(const item of state.points){` 之前插入：

```js
    const highlight=labelHighlightSet();
```

并把盒体材质那行的 opacity 表达式：

```js
opacity:selected?Math.min(.85,state.toleranceOpacity+.25):state.toleranceOpacity,
```

改为：

```js
opacity:selected?Math.min(.85,state.toleranceOpacity+.25):(highlight&&!highlight.has(item.id)?state.toleranceOpacity*.25:state.toleranceOpacity),
```

再在其下方 `const selected=...` 之后插入一行供棱线复用：

```js
      const dimmed=highlight&&!highlight.has(item.id);
```

并把棱线材质的 opacity 从 `opacity:selected?.95:.5` 改为 `opacity:selected?.95:dimmed?.12:.5`。

- [ ] **Step 3c: hover 时重画容差盒**

`pointermove` 处理器里 `state.hoveredId` 变化时已经调 `renderScene()`，而 `renderScene()` 内含 `rebuildTolerance()`——无需额外改动。确认这一点即可，不要重复加调用。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 5: 浏览器 smoke**

切到「标准型号」（需登录），把鼠标悬到一个容差盒与别人重叠的型号上，确认：它与重叠伙伴保持正常亮度，其余型号的标签和容差盒明显变淡。悬到孤立型号上时，全场亮度不变。

- [ ] **Step 6: 提交**

```bash
git add services/public-web/formula/colors/index.html tests/test_formula_color_space_frontend.py
git commit -m "feat(colors): hover 型号时高亮容差重叠伙伴并压暗无关项"
```

---

### Task 9: 搜索命中自动聚焦 + 移动端取证 + 文档闭环

**Files:**
- Modify: `services/public-web/formula/colors/index.html`
- Test: `tests/test_formula_color_space_frontend.py`
- Modify: `docs/project-ai-map.md`

**Interfaces:**
- Consumes: 已有的 `filtersChanged()`、`selectPoint(item)`、`focusSelected()`、`state.points`
- Produces: 无新导出

- [ ] **Step 1: 写失败测试**

在 `tests/test_formula_color_space_frontend.py` 中，`test_highlight_dims_unrelated_tolerance_boxes_too` 之后插入：

```python
    def test_unique_search_hit_flies_the_camera_to_it(self) -> None:
        self.assertIn("state.points.length===1", self.html)
        self.assertIn("selectPoint(state.points[0]);focusSelected()", self.html)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_formula_color_space_frontend.FormulaColorSpaceFrontendTests.test_unique_search_hit_flies_the_camera_to_it -v`
Expected: FAIL

- [ ] **Step 3: 实现**

把这行：

```js
  $('formulaSearch').oninput=(event)=>{state.query=event.target.value.trim();filtersChanged();};
```

改为：

```js
  // 搜到唯一型号就直接飞过去；命中多个时不动相机，否则边打字边跳很晕。
  $('formulaSearch').oninput=(event)=>{state.query=event.target.value.trim();filtersChanged();if(state.query&&state.points.length===1){selectPoint(state.points[0]);focusSelected();}};
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest discover -s tests`
Expected: 全部 PASS

- [ ] **Step 5: 移动端一屏取证**

用 playwright 在 390×844 视口打开页面，分别在面板收起与展开两种状态下量 `document.body.scrollHeight` 与 `window.innerHeight`，确认 `scrollHeight <= innerHeight`。把两组数字贴进 PR 描述。若展开态超出，把 `.settings-panel` 改为 `max-height:38vh;overflow-y:auto`（面板内部滚动，页面不滚动）后重测。

- [ ] **Step 6: 文档闭环**

在 `docs/project-ai-map.md` 中找到 `formula_color_space` / `/formula/colors/` 对应条目，补一句：视图设置已从画布浮层移到顶部 `#settingsPanel`；色点标签由 `rebuildLabels()` / `syncLabels()` 的 DOM 层渲染，偏好存 `localStorage['aliecs_formula_colors_view_prefs']`。

- [ ] **Step 7: 提交并开 PR**

```bash
git add services/public-web/formula/colors/index.html tests/test_formula_color_space_frontend.py docs/project-ai-map.md
git commit -m "feat(colors): 搜索命中唯一型号时自动聚焦"
git push -u origin feat/colors-label-layer-and-settings
gh pr create --title "feat(colors): 色点标签 DOM 化、设置收进顶部面板" --body "$(cat <<'EOF'
## 改动
- 色点标签从 canvas Sprite 换成 DOM 覆盖层，按与 orbit target 的距离做余弦缓动淡化
- 新增「全部显示标签」开关与淡化距离滑块，勾选字段变化时全部标签实时刷新
- 视图设置从画布左下浮层收进顶部 `⚙ 显示设置` 面板，展开时挤压画布高度而非遮挡
- 标签勾选/开关/淡化距离可「设为默认」存 localStorage
- hover 型号时高亮容差重叠伙伴、压暗无关项
- 搜索命中唯一型号时相机自动飞过去

## 参考
标签淡化与三态 opacity 借鉴 HananoshikaYomaru/obsidian-3d-graph 的 `ForceGraphEngine.ts:295-307` 与 `ForceGraph.ts` nodeThreeObject。未引入新依赖（不用 CSS2DRenderer，色点是 InstancedMesh 无独立 Object3D 可挂）。

## 验证
`python -m unittest discover -s tests` 全绿。浏览器 smoke 覆盖标签淡化、面板展开、设为默认往返、hover 高亮、搜索聚焦。移动端 390×844 视口高度取证：<填实测数字>。

Nav-Impact: updated
EOF
)"
```

---

## 自查记录

- **Spec 覆盖**：PR-A 三项改动 → Task 1/2/3；PR-B 的 B1 → Task 4/5，B2 → Task 7，B3 → Task 6，B4 → Task 8，B5 → Task 9。设计文档「验证」一节的每条都落在对应 Task 的 Step 5。
- **既有测试冲突**：`test_unchanged_row_only_refreshes_checked_at`（Task 2 改写）、`test_detail_fields_control_selected_product_3d_label`（Task 4 改写）、`test_view_controls_are_collapsible`（Task 6 改写）。三处都在对应任务的 Step 1 明确替换，不会留下红灯。
- **命名一致性**：`plan_creates` / `created_rows` / `rebuildLabels` / `syncLabels` / `labelHighlightSet` / `labelOpacityBase` / `savePrefs` / `loadPrefs` / `applyPrefsToControls` / `state.labelFocal` / `state.showAllLabels` 在定义处与所有引用处拼写一致。
- **待实跑确认的两处**：`add_records` 是否需要显式 `key_type`（Task 3 Step 3d 已写明失败时的处理）；淡化距离默认值 12 的观感（Task 5 Step 5 实测后可微调常量）。
