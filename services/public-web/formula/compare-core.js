// compare-core.js — 配方对比核心算法（网站 + 微信小程序共享，单一事实源）。
// 权威源：AliECS services/public-web/formula/compare-core.js；
// 本文件是 weapp 副本，勿手改——改权威源后跑 `node scripts/sync-shared.mjs` 同步。
// 纯计算/纯格式化：零 DOM、零存储、零网络。兼容红线：禁 ?. / ?? / Intl（iOS JSCore）。
// 行为单测：weapp-lab tests/compare-core.test.js（node --test）。
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CompareCore = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const DASH = '—';
  const ratio = (value) => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(3)}%` : '';
  const ratioD = (value) => (!Number.isFinite(Number(value)) || Math.abs(Number(value)) < 0.000005) ? DASH : ratio(value);
  const fmtDelta = (value) => `${value > 0 ? '+' : ''}${(Number(value) * 100).toFixed(3)}%`;
  // Intl-free 千分位：十进制定点(6位)进位正确+尾零裁剪。极端边界与旧 Intl 实现可能有
  // 半位差异——网站与小程序共用本实现，两端一致性由构造保证，无跨端漂移。
  const formatQty = (value) => {
    const n = Number(value);
    if (!Number.isFinite(n)) return DASH;
    if (Math.abs(n) >= 1e21) return String(n);
    const fixed = n.toFixed(6);
    const parts = fixed.replace('-', '').split('.');
    const frac = parts[1].replace(/0+$/, '');
    const grouped = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (n < 0 && fixed.indexOf('-') === 0 ? '-' : '') + (frac ? `${grouped}.${frac}` : grouped);
  };
  const shortVersion = (value) => { const text = String(value || ''); return text.length > 18 ? text.replace('2026-', '26-') : text; };
  const rowKey = (row) => `${row.parentCode}|||${row.version}`;
  const mode = (items) => {
    const counts = new Map();
    items.filter((item) => item !== '' && item != null).forEach((item) => counts.set(String(item), (counts.get(String(item)) || 0) + 1));
    const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    return sorted.length ? sorted[0][0] : '';
  };
  const itemPrefix = (code) => String(code || '').slice(0, 3);
  const NON_DISABLED_VALUES = new Set(['', '0', '否', 'False', 'false']);
  const rowDisabled = (row) => !NON_DISABLED_VALUES.has(String((row && ('停用' in row ? row['停用'] : row.disabled)) || '').trim());

  function numberFromCell(value) {
    const text = String(value == null ? '' : value).trim();
    if (!text) return 0;
    const numeric = Number(text.replace('%', ''));
    if (!Number.isFinite(numeric)) return 0;
    return text.indexOf('%') >= 0 ? numeric / 100 : numeric;
  }

  function detailToCompareRow(row) {
    return {
      parentCode: String(row['父件编码'] || ''),
      parentName: String(row['父件名称'] || ''),
      version: String(row['版本号_子件'] || ''),
      itemCode: String(row['子件编码'] || ''),
      itemName: String(row['子件名称'] || ''),
      spec: String(row['规格型号_子件'] || ''),
      unit: String(row['计量单位_子件'] || ''),
      qty: numberFromCell(row['需用数量']),
      ratio: numberFromCell(row['比例']),
      defaultBOM: String(row['默认BOM'] || ''),
      disabled: String(row['停用'] || ''),
    };
  }

  function majorityPatterns(rows) {
    return {
      parentCode: mode(rows.map((row) => row.parentCode)),
      itemLen: Number(mode(rows.map((row) => String(row.itemCode).length))) || 0,
      itemPrefix: mode(rows.map((row) => itemPrefix(row.itemCode))),
    };
  }
  function isSpecialParentCode(code, majority) {
    return Boolean(majority.parentCode && String(code) !== String(majority.parentCode));
  }
  function isSpecialItemCode(code, majority) {
    const text = String(code || '');
    return Boolean((majority.itemLen && text.length !== majority.itemLen) || (majority.itemPrefix && itemPrefix(text) !== majority.itemPrefix));
  }
  function codeTip(code, majority) {
    const text = String(code || '');
    const reasons = [];
    if (majority.itemLen && text.length !== majority.itemLen) reasons.push(`长度${text.length}位，多数为${majority.itemLen}位`);
    if (majority.itemPrefix && itemPrefix(text) !== majority.itemPrefix) reasons.push(`前缀${itemPrefix(text)}，多数前缀${majority.itemPrefix}`);
    return reasons.join('；');
  }

  function buildVersions(rows) {
    const map = new Map();
    rows.forEach((row) => {
      const key = rowKey(row);
      if (!map.has(key)) map.set(key, { key, parentCode: row.parentCode, parentName: row.parentName, version: row.version, defaultBOM: '', disabled: '1', rows: [], maxRatio: 0 });
      const version = map.get(key);
      version.rows.push(row);
      version.maxRatio = Math.max(version.maxRatio, row.ratio || 0);
      if (String(row.defaultBOM) === '1') version.defaultBOM = '1';
      if (!rowDisabled(row)) version.disabled = '0';
    });
    const versions = [...map.values()];
    const selectedKeys = new Set(versions.map((version) => version.key));
    const defaultVersion = versions.find((version) => version.defaultBOM === '1') || versions[0] || null;
    const enabledVersion = versions.find((version) => !rowDisabled(version)) || versions[versions.length - 1] || defaultVersion;
    const baseKey = defaultVersion ? defaultVersion.key : '';
    const targetCandidates = versions.filter((version) => version.key !== baseKey);
    const targetKeys = new Set((targetCandidates.length ? targetCandidates : [enabledVersion || defaultVersion]).filter(Boolean).map((version) => version.key));
    return { versions, selectedKeys, baseKey, targetKeys };
  }

  // sel = { versions, selectedKeys:Set, baseKey, targetKeys:Set }
  function selectedVersions(sel) {
    return sel.versions.filter((version) => sel.selectedKeys.has(version.key));
  }
  function baseVersion(sel) {
    return sel.versions.find((version) => version.key === sel.baseKey) || selectedVersions(sel)[0] || sel.versions[0] || null;
  }
  function normalizeTargets(sel) {
    const selected = selectedVersions(sel), selectedKeys = new Set(selected.map((version) => version.key)), base = baseVersion(sel);
    sel.targetKeys = new Set([...sel.targetKeys].filter((key) => selectedKeys.has(key)));
    if (base && selected.length > 1) sel.targetKeys.delete(base.key);
    if (!sel.targetKeys.size) {
      const fallback = selected.filter((version) => !base || version.key !== base.key);
      sel.targetKeys = new Set((fallback.length ? fallback : selected).map((version) => version.key));
    }
  }
  function targetVersions(sel) {
    const selected = selectedVersions(sel);
    if (!selected.length) return [];
    const base = baseVersion(sel);
    const explicit = selected.filter((version) => sel.targetKeys.has(version.key));
    if (explicit.length) return explicit;
    const fallback = selected.filter((version) => !base || version.key !== base.key);
    return fallback.length ? fallback : (base ? [base] : selected);
  }
  function isTargetVersion(sel, version) {
    return targetVersions(sel).some((target) => target.key === version.key);
  }
  const targetLabel = (version) => `${version.parentCode}｜${version.version || '-'}`;
  function versionMaxRatio(sel, key) {
    const found = sel.versions.find((version) => version.key === key);
    return (found && found.maxRatio) || 1;
  }

  function itemFamily(row) {
    const text = (row.itemName || row.spec || row.itemCode).replace(/[0-9A-Za-z\-_\s（）()]/g, '');
    return `${row.unit}::${text.slice(0, 3) || row.itemCode.slice(0, 3)}`;
  }
  function buildCompareMatrix(queryRows, sel) {
    const map = new Map();
    queryRows.forEach((row) => {
      const key = rowKey(row);
      if (!sel.selectedKeys.has(key)) return;
      if (!map.has(row.itemCode)) map.set(row.itemCode, { itemCode: row.itemCode, itemName: row.itemName, spec: row.spec, unit: row.unit, family: itemFamily(row), cells: {} });
      map.get(row.itemCode).cells[key] = row;
    });
    return [...map.values()].sort((a, b) => a.itemCode.localeCompare(b.itemCode, 'zh-CN'));
  }
  function categoryHasReplacement(row, rows, target, sel) {
    const base = baseVersion(sel);
    if (!base || !target) return false;
    const peers = rows.filter((item) => item.family === row.family);
    if (peers.length < 2) return false;
    const baseCodes = peers.filter((item) => item.cells[base.key]).map((item) => item.itemCode).sort().join(',');
    const targetCodes = peers.filter((item) => item.cells[target.key]).map((item) => item.itemCode).sort().join(',');
    return Boolean(baseCodes && targetCodes && baseCodes !== targetCodes);
  }
  function rowStatusForTarget(row, rows, target, sel) {
    const base = baseVersion(sel);
    const baseCell = base ? row.cells[base.key] : null;
    const targetCell = target ? row.cells[target.key] : null;
    if (categoryHasReplacement(row, rows, target, sel) && (!baseCell || !targetCell)) return 'replace';
    if (!baseCell && targetCell) return 'add';
    if (baseCell && !targetCell) return 'del';
    if (baseCell && targetCell && Math.abs(targetCell.ratio - baseCell.ratio) > 0.000001) return 'change';
    const values = [baseCell ? baseCell.ratio : NaN, targetCell ? targetCell.ratio : NaN].filter(Number.isFinite);
    const unique = [...new Set(values.map((value) => value.toFixed(6)))];
    if (values.length && unique.length === 1) return 'same';
    if (!baseCell && !targetCell) return 'history';
    return 'same';
  }
  function rowStatus(row, rows, sel) {
    const targets = targetVersions(sel);
    if (!targets.length) return rowStatusForTarget(row, rows, baseVersion(sel), sel);
    const statuses = targets.map((target) => rowStatusForTarget(row, rows, target, sel));
    const priority = ['replace', 'add', 'del', 'change', 'same', 'history'];
    return priority.find((status) => statuses.includes(status)) || 'same';
  }
  const statusText = { change: '变更', replace: '替换', add: '新增', del: '删除', same: '一致', history: '历史' };
  const statusClass = { change: 'st-change', replace: 'st-replace', add: 'st-add', del: 'st-del', same: 'st-same', history: 'st-history' };
  const EXPORT_ST_ORDER = { replace: 0, add: 1, del: 2, change: 3, same: 4, history: 5 };
  const FILTER_LABELS = { all: '全部', diff: '仅差异', replace: '仅替换', adddel: '仅新增/删除', change: '仅比例变化', same: '仅一致' };
  function passCompareFilter(status, activeFilter) {
    return activeFilter === 'all' || (activeFilter === 'diff' && status !== 'same') || (activeFilter === 'replace' && status === 'replace') || (activeFilter === 'adddel' && (status === 'add' || status === 'del')) || (activeFilter === 'change' && status === 'change') || (activeFilter === 'same' && status === 'same');
  }
  const VIEW_DEFAULTS = { spec: true, qty: true, pct: true, arrow: true, delta: true, newTag: true, bar: true };

  function applyQuickSelect(sel, quickMode) {
    const all = sel.versions.map((version) => version.key);
    if (quickMode === 'all') sel.selectedKeys = new Set(all);
    else if (quickMode === 'none') sel.selectedKeys = new Set();
    else if (quickMode === 'invert') sel.selectedKeys = new Set(all.filter((key) => !sel.selectedKeys.has(key)));
    else if (quickMode === 'default') sel.selectedKeys = new Set(sel.versions.filter((version) => version.defaultBOM === '1').map((version) => version.key));
    else if (quickMode === 'activeDefault') sel.selectedKeys = new Set(sel.versions.filter((version) => !rowDisabled(version) || version.defaultBOM === '1').map((version) => version.key));
    if (!sel.selectedKeys.has(sel.baseKey)) {
      const first = sel.versions.find((version) => sel.selectedKeys.has(version.key));
      sel.baseKey = first ? first.key : '';
    }
    normalizeTargets(sel);
  }

  function buildComparePayload(args) {
    const query = args.query, activeFilter = args.activeFilter, view = args.view, queryRows = args.queryRows, sel = args.sel, majority = args.majority;
    const selected = selectedVersions(sel);
    if (!selected.length) return null;
    const base = baseVersion(sel), targets = targetVersions(sel);
    if (!base || !targets.length) return null;
    const orderOf = (st) => (st in EXPORT_ST_ORDER ? EXPORT_ST_ORDER[st] : 9);
    const rowsAll = buildCompareMatrix(queryRows, sel);
    const rows = rowsAll.map((row) => Object.assign({}, row, { st: rowStatus(row, rowsAll, sel) }))
      .filter((row) => passCompareFilter(row.st, activeFilter))
      .sort((a, b) => (orderOf(a.st) - orderOf(b.st)) || a.itemCode.localeCompare(b.itemCode, 'zh-CN'));
    return {
      query,
      filter_label: FILTER_LABELS[activeFilter] || activeFilter,
      view,
      versions: selected.map((version) => ({ label: targetLabel(version), code: version.parentCode, version: version.version || '', is_base: version.key === base.key, is_target: targets.some((target) => target.key === version.key) })),
      rows: rows.map((row) => ({
        status: row.st,
        item_code: row.itemCode,
        item_name: row.itemName,
        spec: row.spec || '',
        unit: row.unit || '',
        code_warn: isSpecialItemCode(row.itemCode, majority),
        cells: selected.map((version) => {
          const cell = row.cells[version.key];
          if (!cell) return null;
          const baseCell = row.cells[base.key];
          const isBase = version.key === base.key;
          const isTarget = targets.some((target) => target.key === version.key);
          return {
            ratio: cell.ratio,
            qty: cell.qty,
            delta: (!isBase && isTarget && baseCell) ? cell.ratio - baseCell.ratio : null,
            is_new: Boolean(!isBase && isTarget && !baseCell),
          };
        }),
      })),
    };
  }

  return {
    DASH, ratio, ratioD, fmtDelta, formatQty, shortVersion, rowKey, mode, itemPrefix, rowDisabled,
    numberFromCell, detailToCompareRow, majorityPatterns, isSpecialParentCode, isSpecialItemCode, codeTip,
    buildVersions, selectedVersions, baseVersion, normalizeTargets, targetVersions, isTargetVersion, targetLabel, versionMaxRatio,
    itemFamily, buildCompareMatrix, categoryHasReplacement, rowStatusForTarget, rowStatus,
    statusText, statusClass, EXPORT_ST_ORDER, FILTER_LABELS, passCompareFilter, VIEW_DEFAULTS,
    applyQuickSelect, buildComparePayload,
  };
}));
