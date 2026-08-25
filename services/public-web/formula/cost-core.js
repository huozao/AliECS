// cost-core.js — 成本核算的纯计算层（本地重算、多选对比矩阵、预估利润）。
// 按 compare-core.js 的同一套「页面薄壳化」做法抽出：零 DOM、零存储、零网络，可用 node --test 直跑。
// 与 compare-core.js 的区别：成本核算受 RBAC（formula.cost.calculate）门控、不在小程序里，
// 所以本文件没有跨端副本，改动不需要同步小程序仓。
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CostCore = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const DASH = '—';
  const num = (value) => {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  };
  const isBlank = (value) => value == null || value === '';

  // 当下价格 / 模拟数量 编辑后在浏览器本地重算（与后端 calculate_recipe_costs 公式一致），
  // 避免每次都请求后端重新解析 Excel。
  function recomputeRecipeCosts(recipe, manualPrices, simulatedQuantities) {
    const lines = (recipe && recipe.lines) || [];
    const prices = manualPrices || {};
    const quantities = simulatedQuantities || {};
    let denom = 0;
    lines.forEach((line) => {
      const manual = prices[line.child_code];
      line.current_price = isBlank(manual) ? num(line.system_price) : Number(manual);
      line.manual_price_applied = !isBlank(manual);
      const sim = quantities[line.child_code];
      line.simulated_quantity = isBlank(sim) ? num(line.quantity) : Number(sim);
      if (!line.excluded) denom += num(line.cost_unit_factor || 1) * num(line.simulated_quantity);
    });
    let systemTotal = 0, currentTotal = 0, simulatedTotal = 0;
    lines.forEach((line) => {
      line.current_amount = num(line.ratio) * num(line.current_price);
      const simCostQty = num(line.cost_unit_factor || 1) * num(line.simulated_quantity);
      line.simulated_ratio = (line.excluded || denom <= 0) ? 0 : simCostQty / denom;
      line.simulated_amount = line.simulated_ratio * num(line.current_price);
      systemTotal += num(line.system_amount);
      currentTotal += num(line.current_amount);
      simulatedTotal += num(line.simulated_amount);
    });
    recipe.system_total = systemTotal;
    recipe.current_total = currentTotal;
    recipe.simulated_total = simulatedTotal;
    return recipe;
  }

  // 多选对比的子件矩阵：行 = 所选配方 lines 的并集，列 = 配方。
  // 排序与 compare-core.buildCompareMatrix 一致（子件编码 zh-CN），保证两张对比表行序一致。
  function buildCostMatrix(recipes, selectedKeys) {
    const keys = selectedKeys instanceof Set ? selectedKeys : new Set(selectedKeys || []);
    const map = new Map();
    (recipes || []).forEach((recipe) => {
      if (!keys.has(recipe.key)) return;
      (recipe.lines || []).forEach((line) => {
        if (!map.has(line.child_code)) {
          map.set(line.child_code, {
            child_code: line.child_code,
            child_name: line.child_name,
            spec: line.spec || '',
            unit: line.unit || '',
            excluded: Boolean(line.excluded),
            cells: {},
          });
        }
        const row = map.get(line.child_code);
        // 名称/规格取第一个非空值：同一子件在不同版本里偶有空字段。
        if (!row.child_name && line.child_name) row.child_name = line.child_name;
        if (!row.spec && line.spec) row.spec = line.spec;
        if (!row.unit && line.unit) row.unit = line.unit;
        row.cells[recipe.key] = line;
      });
    });
    return [...map.values()].sort((a, b) => String(a.child_code).localeCompare(String(b.child_code), 'zh-CN'));
  }

  // 预估利润。主口径是毛利率（÷ 销售价格），同时给出成本加成率（÷ 成本）。
  // 销售价格缺失、或分母为 0 时返回 null，由调用方显示 DASH——不做 0 兜底，避免把「没有数据」
  // 和「利润恰好是 0」显示成同一个东西。
  // ⚠️ 一律走 numOrNaN：Number(null) 是 0 不是 NaN，后端「无销售价记录」返回的 sales_price:null
  // 会被当成售价 0，把「没有数据」显示成 -100% 的利润。
  const numOrNaN = (value) => (isBlank(value) ? NaN : Number(value));

  function profitMetrics(args) {
    const input = args || {};
    const salesPrice = numOrNaN(input.salesPrice);
    const cost = numOrNaN(input.cost);
    const otherCost = num(input.otherCost);
    if (!Number.isFinite(salesPrice) || !Number.isFinite(cost)) return { profit: null, margin: null, markup: null };
    const profit = salesPrice - cost - otherCost;
    return {
      profit,
      margin: salesPrice > 0 ? profit / salesPrice : null,
      markup: cost > 0 ? profit / cost : null,
    };
  }

  const pct = (value) => (value == null || !Number.isFinite(value)) ? DASH : `${value >= 0 ? '+' : ''}${(value * 100).toFixed(1)}%`;
  const fixed3 = (value) => (Number.isFinite(Number(value)) ? Number(value).toFixed(3) : DASH);

  // 悬浮提示：三个成本口径各代入实际数字的两条算式 + 成本口径说明，让用户知道数字是怎么来的。
  function profitTooltipText(args) {
    const input = args || {};
    const salesPrice = numOrNaN(input.salesPrice);
    const otherCost = num(input.otherCost);
    if (!Number.isFinite(salesPrice)) {
      return '该成品没有销售价记录，也未手填「模拟销售价格」，无法估算利润。\n在上方「模拟销售价格」里填一个价格即可看到毛利率。';
    }
    const lines = [];
    [['系统', input.systemCost], ['当前', input.currentCost], ['模拟', input.simulatedCost]].forEach((pair) => {
      const label = pair[0], cost = numOrNaN(pair[1]);
      if (!Number.isFinite(cost)) return;
      const metrics = profitMetrics({ salesPrice, cost, otherCost });
      const head = `（${fixed3(salesPrice)} − ${fixed3(cost)} − ${fixed3(otherCost)}）`;
      lines.push(`${label}毛利率 = ${head} ÷ ${fixed3(salesPrice)} = ${pct(metrics.margin)}`);
      lines.push(`${label}加成率 = ${head} ÷ ${fixed3(cost)} = ${pct(metrics.markup)}`);
    });
    lines.push('');
    lines.push('成本口径：合价只含进比例分母的原料（每 kg）。');
    lines.push('包装袋、色粉包等不计比例的子件不在内，请计入「其他综合成本」。');
    return lines.join('\n');
  }

  // 汇总矩阵的行内最优/最差标记。成本类越低越优，利润类越高越优；
  // 只有 2 个以上不同的有限值才标，否则全列一样时标出来没有意义。
  function bestWorstIndexes(values, lowerIsBetter) {
    const usable = [];
    (values || []).forEach((value, index) => {
      if (value != null && Number.isFinite(Number(value))) usable.push({ index, value: Number(value) });
    });
    if (usable.length < 2) return { best: -1, worst: -1 };
    const distinct = new Set(usable.map((item) => item.value.toFixed(6)));
    if (distinct.size < 2) return { best: -1, worst: -1 };
    const sorted = [...usable].sort((a, b) => a.value - b.value);
    const low = sorted[0].index, high = sorted[sorted.length - 1].index;
    return lowerIsBetter ? { best: low, worst: high } : { best: high, worst: low };
  }

  return { DASH, recomputeRecipeCosts, buildCostMatrix, profitMetrics, profitTooltipText, bestWorstIndexes, pct, fixed3 };
}));
