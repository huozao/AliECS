from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


FORMULA_DIR = Path(__file__).resolve().parents[1] / "services" / "public-web" / "formula"
COST_CORE = FORMULA_DIR / "cost-core.js"
COMPARE_CORE = FORMULA_DIR / "compare-core.js"

# cost-core.js / compare-core.js 都是纯计算模块（零 DOM/存储/网络），可以直接在 node 里 require 出来断言。
# 本仓没有 node 测试基建，CI 只跑 unittest——所以用 Python 驱动 node，node 不在 PATH 时跳过，
# 契约层面的兜底断言仍留在 test_formula_frontend.py。
HARNESS = r"""
const assert = require('assert');
const CostCore = require(process.argv[process.argv.length - 2]);
const CompareCore = require(process.argv[process.argv.length - 1]);
const out = [];
function check(name, fn) { fn(); out.push(name); }

// 对比表排序的共用样本：目标版本 T、基准版本 B；BAG1 是不计比例的包装袋（ratio 为 0）。
function mkRows() {
  return [
    { itemCode: 'CHG1', itemName: '沉淀硫酸钡', family: 'kg::沉淀硫', st: 'change', cells: { T: { ratio: 0.1 }, B: { ratio: 0.3 } } },
    { itemCode: 'ADD1', itemName: '钛白粉', family: 'kg::钛白粉', st: 'add', cells: { T: { ratio: 0.5 } } },
    { itemCode: 'DEL1', itemName: 'ABS树脂', family: 'kg::树脂', st: 'del', cells: { B: { ratio: 0.2 } } },
    { itemCode: 'BAG1', itemName: '有字包装袋', family: '条::有字包', st: 'same', cells: { T: { ratio: 0 }, B: { ratio: 0 } } },
  ];
}
const ratioOf = (row) => (row.cells.T ? row.cells.T.ratio : (row.cells.B ? row.cells.B.ratio : NaN));

check('profit_metrics_both_calibers', () => {
  const m = CostCore.profitMetrics({ salesPrice: 17, cost: 12.88, otherCost: 0.8 });
  assert.ok(Math.abs(m.profit - 3.32) < 1e-9);
  assert.ok(Math.abs(m.margin - 3.32 / 17) < 1e-12);
  assert.ok(Math.abs(m.markup - 3.32 / 12.88) < 1e-12);
});

check('profit_metrics_null_when_no_sales_price', () => {
  const m = CostCore.profitMetrics({ salesPrice: null, cost: 12.88, otherCost: 0 });
  assert.strictEqual(m.margin, null);
  assert.strictEqual(m.markup, null);
  assert.strictEqual(m.profit, null);
});

check('profit_metrics_zero_denominators_are_null_not_zero', () => {
  const zeroSales = CostCore.profitMetrics({ salesPrice: 0, cost: 5, otherCost: 0 });
  assert.strictEqual(zeroSales.margin, null);
  assert.ok(Math.abs(zeroSales.markup - (-1)) < 1e-12);
  const zeroCost = CostCore.profitMetrics({ salesPrice: 10, cost: 0, otherCost: 0 });
  assert.strictEqual(zeroCost.markup, null);
  assert.ok(Math.abs(zeroCost.margin - 1) < 1e-12);
});

check('profit_metrics_negative_is_kept', () => {
  const m = CostCore.profitMetrics({ salesPrice: 10, cost: 12, otherCost: 1 });
  assert.ok(m.profit < 0 && m.margin < 0);
});

check('tooltip_contains_four_formulas', () => {
  const text = CostCore.profitTooltipText({ salesPrice: 17, currentCost: 12.88, simulatedCost: 12.6, otherCost: 0.8 });
  ['当前毛利率', '当前加成率', '模拟毛利率', '模拟加成率'].forEach((label) => assert.ok(text.includes(label), label));
  assert.ok(text.includes('其他综合成本'));
});

check('tooltip_explains_missing_sales_price', () => {
  const text = CostCore.profitTooltipText({ salesPrice: null, currentCost: 12.88, otherCost: 0 });
  assert.ok(text.includes('没有销售价记录'));
});

check('build_cost_matrix_unions_and_sorts', () => {
  const recipes = [
    { key: 'A', lines: [{ child_code: 'C200', child_name: '色粉', spec: '', unit: 'g' }, { child_code: 'C100', child_name: '树脂', spec: 'S', unit: 'kg' }] },
    { key: 'B', lines: [{ child_code: 'C300', child_name: '助剂', spec: 'T', unit: 'kg' }, { child_code: 'C100', child_name: '树脂', spec: 'S', unit: 'kg' }] },
    { key: 'C', lines: [{ child_code: 'ZZZ', child_name: '未选中', spec: '', unit: 'kg' }] },
  ];
  const rows = CostCore.buildCostMatrix(recipes, new Set(['A', 'B']));
  assert.deepStrictEqual(rows.map((r) => r.child_code), ['C100', 'C200', 'C300']);
  assert.deepStrictEqual(Object.keys(rows[0].cells).sort(), ['A', 'B']);
  assert.deepStrictEqual(Object.keys(rows[1].cells), ['A']);
});

check('recompute_matches_backend_formula', () => {
  const recipe = { lines: [
    { child_code: 'C100', quantity: 20, cost_unit_factor: 1, excluded: false, ratio: 0.8, system_price: 3, system_amount: 2.4 },
    { child_code: 'C200', quantity: 5000, cost_unit_factor: 0.001, excluded: false, ratio: 0.2, system_price: 4, system_amount: 0.8 },
    { child_code: 'BAG', quantity: 40, cost_unit_factor: 1, excluded: true, ratio: 0, system_price: 1, system_amount: 0 },
  ] };
  CostCore.recomputeRecipeCosts(recipe, { C100: 5 }, {});
  assert.ok(Math.abs(recipe.current_total - (0.8 * 5 + 0.2 * 4)) < 1e-12);
  // 排除行不进模拟分母：20kg + 5kg = 25kg
  const bag = recipe.lines[2];
  assert.strictEqual(bag.simulated_ratio, 0);
  assert.ok(Math.abs(recipe.lines[0].simulated_ratio - 0.8) < 1e-12);
});

check('best_worst_needs_two_distinct_values', () => {
  assert.deepStrictEqual(CostCore.bestWorstIndexes([5, 5, 5], true), { best: -1, worst: -1 });
  assert.deepStrictEqual(CostCore.bestWorstIndexes([5, null], true), { best: -1, worst: -1 });
  assert.deepStrictEqual(CostCore.bestWorstIndexes([9, 3, 7], true), { best: 1, worst: 0 });
  assert.deepStrictEqual(CostCore.bestWorstIndexes([9, 3, 7], false), { best: 0, worst: 1 });
});

check('sort_default_groups_by_status', () => {
  const rows = mkRows();
  const order = CompareCore.sortCompareRows(rows, { sortKey: 'default', ratioOf }).map((r) => r.itemCode);
  assert.deepStrictEqual(order, ['ADD1', 'DEL1', 'CHG1', 'BAG1']);
});

check('sort_by_code_and_reverse', () => {
  const rows = mkRows();
  const asc = CompareCore.sortCompareRows(rows, { sortKey: 'code', ratioOf }).map((r) => r.itemCode);
  const desc = CompareCore.sortCompareRows(rows, { sortKey: 'code', sortDir: 'desc', ratioOf }).map((r) => r.itemCode);
  assert.deepStrictEqual(asc, [...asc].sort());
  assert.deepStrictEqual(desc, [...asc].reverse());
});

check('sort_by_ratio_keeps_zero_ratio_rows_last_in_both_directions', () => {
  // 回归：包装袋等不计比例的行 ratio 是 0（numberFromCell 把空值转 0）而不是 NaN。
  // 只判 isFinite 会把它们当成「占比 0」排进队列，倒序时翻到最前。
  const rows = mkRows();
  const asc = CompareCore.sortCompareRows(rows, { sortKey: 'ratio', ratioOf }).map((r) => r.itemCode);
  const desc = CompareCore.sortCompareRows(rows, { sortKey: 'ratio', sortDir: 'desc', ratioOf }).map((r) => r.itemCode);
  assert.strictEqual(asc[asc.length - 1], 'BAG1');
  assert.strictEqual(desc[desc.length - 1], 'BAG1');
  assert.strictEqual(asc[0], 'ADD1');       // 0.5 最大
  assert.strictEqual(desc[0], 'CHG1');      // 0.1 最小
});

check('sort_by_ratio_falls_back_to_base_when_missing_in_target', () => {
  // 目标里被删掉的物料若没有回落，会全掉到最后，比例排序就看不出它原来占多少
  const rows = mkRows();
  const order = CompareCore.sortCompareRows(rows, { sortKey: 'ratio', ratioOf }).map((r) => r.itemCode);
  assert.ok(order.indexOf('DEL1') < order.indexOf('CHG1'));
});

check('sort_by_family_groups_replaceable_items', () => {
  const rows = mkRows();
  const order = CompareCore.sortCompareRows(rows, { sortKey: 'family', ratioOf }).map((r) => r.itemCode);
  assert.strictEqual(order.length, 4);
  assert.ok(order.indexOf('BAG1') === 0 || order.indexOf('BAG1') === 3);
});

console.log(JSON.stringify(out));
"""


class FormulaCoreJsTests(unittest.TestCase):
    def test_cost_core_pure_functions(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不在 PATH，跳过 cost-core.js 算法断言")
        result = subprocess.run(
            [node, "-e", HARNESS, "--", str(COST_CORE), str(COMPARE_CORE)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, msg=result.stderr[-2000:])
        passed = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(14, len(passed), msg=f"实际通过：{passed}")


if __name__ == "__main__":
    unittest.main()
