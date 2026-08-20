# 配方成本核算：单位判据反转 + 多选对比 + 预估利润

日期：2026-08-20
页面：`https://hydwang.xyz/formula/`
涉及仓库：AliECS（后端 `services/backend-api`、前端 `services/public-web/formula`）

三项互相独立，可分三次交付；顺序建议 1 → 3 → 2（第 2 项的汇总矩阵要复用第 3 项的利润算法）。

---

## 变更 1：比例分母判据由黑名单反转为白名单

### 问题（已取证）

生产 BOM `bom_20260819_170346.xlsx`（txecs `business-cn-backend-api-1`）单位分布：

| 单位 | 行数 | 当前是否进分母 |
|---|---|---|
| kg | 1920 | 是 |
| 条 | 231 | 否（SKIP_RATIO_UNITS） |
| 个 | 36 | 否（SKIP_RATIO_UNITS） |
| 克 | 3 | 是（换算 ÷1000） |
| 份 | 1 | **是（错误）** |

「份」只有一个子件：色粉包，只出现在一本成品的默认 BOM 里。该配方的 kg 子件合计 **100.5 kg**，
另有色粉包 1 份、包装袋 4 条。

分母现为 **101.5**——色粉包的「1 份」被当成 1 kg 加进了分母；应为 **100.5**。
所有 kg 子件的比例因此被压低约 1%，色粉包自己还拿到一个 0.985% 的假比例。

（本仓为 PUBLIC，配方组成与逐项用量不落库；复核用只读命令见下方「验证」。）

### 根因

`services/backend-api/app/recipes/bom_query.py:112` 用黑名单枚举非质量单位：

```python
SKIP_RATIO_UNITS = {"条", "个"}
```

这是第二次因枚举漏项而静默出错（第一次是 HYD-0601 新包装袋，才补的按单位兜底）。黑名单的失效是无声的——比例只是被稀释，页面不会报任何异常。

### 方案

反转为白名单：只有可换算成质量的单位进分母。

```python
KG_UNITS = {"kg", "KG", "Kg", "kG", "千克", "公斤"}
GRAM_UNITS = {"克", "g", "G", "gram", "grams"}      # 已存在，保留
MASS_UNITS = KG_UNITS | GRAM_UNITS
```

- 「吨」**不进**白名单。`_cost_quantity()` 只有克→千克的换算因子，没有吨的；放进白名单会算错一个数量级，排除并告警反而安全。
- `_ratio_excluded(child_code, unit)` 改为 `child_code in SKIP_CHILD_CODES or unit not in MASS_UNITS`。
- `SKIP_RATIO_UNITS` 降级为 `KNOWN_NON_MASS_UNITS = {"条", "个", "份"}`，只用于告警去噪，不再参与判定。

### 必须处理的反转陷阱

`compute_ratio_series()` 现有写法：

```python
if "计量单位_子件" in df.columns:
    mask &= ~unit.isin(SKIP_RATIO_UNITS)
```

黑名单下「缺单位列」＝不排除任何行（安全降级）；白名单下同一个写法会变成**排除所有行**，整批配方比例全空。反转时必须显式保留缺列降级：

```python
if "计量单位_子件" in df.columns:
    mask &= unit.isin(MASS_UNITS)
# 缺列时不按单位过滤，行为与反转前一致
```

### 告警

`compute_ratio_series()` 里对 `unit` 取 distinct，凡不在 `MASS_UNITS ∪ KNOWN_NON_MASS_UNITS` 的值打一条 WARNING（每次查询最多一条，列出新单位集合）。这是白名单方案唯一的兜底：T+ 若改用未覆盖的质量写法，日志会先叫，而不是等人发现比例塌了。

### 副作用

色粉包的「系统分价 / 当下分价 / 模拟分价」归 0（`比例` 为空 → `_float_or_zero` → 0），与包装袋当前行为一致。即「当前成本合价」= 每 kg 原料成本，不含包装与色粉包。这一点由变更 3 的「其他综合成本」承接，并写进 tooltip 说明。

### 验证

`tests/test_recipe_query.py` 新增：

1. 单位为「份」的子件不进分母，其余 kg 子件比例之和为 1。
2. **反证**：把判据临时换回 `SKIP_RATIO_UNITS = {"条","个"}` 时该用例必须失败——确认新测试真的挡得住这个 bug，而不是恰好通过。
3. 单位变体覆盖：`kg / KG / 千克 / 公斤 / 克 / g` 都进分母；`吨 / 包 / 只 / PCS` 都不进。
4. 缺 `计量单位_子件` 列时不按单位过滤（回归防线）。

改动文件：`services/backend-api/app/recipes/bom_query.py`、`tests/test_recipe_query.py`。

---

## 变更 3：模拟销售价格 + 其他综合成本 + 预估利润

（先于变更 2 交付，因为变更 2 的汇总矩阵要复用这里的利润算法。）

### 改名

| 现 | 改为 |
|---|---|
| 销售价格 | 系统销售价格 |
| 当前合价 | 当前成本合价 |

### 两个新输入框

完全复用「当下价格」列的记忆语义：localStorage 持久化 + 时间戳 + 悬浮显示更新时间 + 值回到默认值时自动删除覆盖。

| 项 | 存储 key（值） | 存储 key（时间） | 记忆粒度 | 默认值 |
|---|---|---|---|---|
| 模拟销售价格 | `formula_sim_sales_prices` | `formula_sim_sales_price_times` | 父件编码 | = 系统销售价格 |
| 其他综合成本 | `formula_other_costs` | `formula_other_cost_times` | 父件编码::版本（＝ `recipe.key`） | 0 |

粒度理由：售价按成品定，与 BOM 版本无关；其他综合成本（包装、色粉包、人工）可能随版本变，多选对比时每列要能独立填，否则该行三列恒等、对比无意义。

校验沿用现有 `onPriceChange`：非负数字，否则 toast 报错并重渲染。

### 预估利润

主口径 **毛利率**（÷ 销售价格），tooltip 同时给出成本加成率（÷ 成本）。当前 / 模拟两行都算：

```
毛利率 = (模拟销售价格 − 成本 − 其他综合成本) ÷ 模拟销售价格
加成率 = (模拟销售价格 − 成本 − 其他综合成本) ÷ 成本

当前行：成本 = 当前成本合价
模拟行：成本 = 模拟合价
```

卡片形态：

```
┌────────────────────────┐
│ 预估利润（毛利率）        │
│ 当前 19.5% · 模拟 21.2%  │
└────────────────────────┘
```

悬浮 tooltip（代入实际数字，四条算式 + 口径说明）：

```
当前毛利率 =（17.00 − 12.88 − 0.80）÷ 17.00 = 19.5%
当前加成率 =（17.00 − 12.88 − 0.80）÷ 12.88 = 25.8%
模拟毛利率 =（17.00 − 12.60 − 0.80）÷ 17.00 = 21.2%
模拟加成率 =（17.00 − 12.60 − 0.80）÷ 12.60 = 28.6%

成本口径：合价只含进比例分母的原料（每 kg）。
包装袋、色粉包等不计比例的子件不在内，请计入「其他综合成本」。
```

边界：

- 系统销售价格缺失（`sales_price` 为 null）且用户未手填 → 利润显示 `—`，tooltip 提示「该成品无销售价记录，请手填模拟销售价格」。
- 模拟销售价格 = 0 → 毛利率分母为 0 → 显示 `—`。
- 成本 = 0 → 加成率分母为 0 → tooltip 中该行显示 `—`，毛利率仍正常算。
- 利润为负正常显示（红色），不做钳制。

### 指标卡布局

从 4 个变 7 个，顺序：

```
系统销售价格 | 模拟销售价格 | 系统合价 | 模拟合价 | 当前成本合价 | 其他综合成本 | 预估利润
```

CSS `.total-row` 的 `grid-template-columns:1.3fr repeat(3,1fr)` 改为 `repeat(auto-fit, minmax(180px, 1fr))`；900px 以下的 `1fr` 单列规则保留。

### tooltip 组件

现有 `.price-cell[data-tooltip]` 是 `white-space:nowrap` 单行的，算式装不下。新增可换行变体 `.calc-tip[data-tooltip]`：`white-space:pre-line`、`max-width:420px`、`text-align:left`，其余（定位、箭头、配色）复用现有规则。

### 改动文件

`services/public-web/formula/index.html`（CSS + 指标卡渲染 + 两个输入框的 change/keydown/持久化）、新增 `services/public-web/formula/cost-core.js`（见下）、`tests/test_formula_frontend.py`。

---

## 变更 2：成本核算多选对比

### 数据层：不用改后端

`POST /v1/recipes/cost` 已经一次性返回查询命中的**全部** recipes（每个「父件编码 × 版本」一条，含完整 `lines`），前端 `state.cost.recipes` 里都在。现在只是 `versionTabs` 做成了单选。多选对比是纯前端改造。

### 模式切换

`versionTabs` 上方加一组 pill：`单本` / `对比`。

- **单本**：现状不变（完整可编辑表格）。
- **对比**：`versionTabs` 变成多选 chips（点击 toggle，选中高亮），附快捷按钮 `全选` / `全不选` / `仅默认BOM`。

### 对比模式的两块内容

**① 汇总矩阵**（行 = 指标，列 = 配方）——多选对比里最有价值的部分：

```
                        成品甲·版本A       成品甲·版本B      成品乙·版本C
系统合价                    12.34             12.10            13.02
当前成本合价                12.88             12.55            13.40
模拟合价                    12.60               —                —
系统销售价格                16.30             16.30            17.00
模拟销售价格 [input]        17.00             16.30            17.00
其他综合成本 [input]         0.80              0.80             0.90
预估利润（当前/模拟）    19.5% / 21.2%     17.9% / —        16.9% / —
```

着色规则：只对 `系统合价 / 当前成本合价 / 模拟合价 / 预估利润` 四行做行内最优（绿）/ 最差（红）标记；成本类越低越优，利润类越高越优。销售价格与其他综合成本是输入项，不比较。

**② 子件矩阵**（行 = 子件并集，列 = 配方 × 要素列组）：

- 行来源：所选 recipes 的 `lines` 按 `child_code` 取并集，`localeCompare` 按 `zh-CN` 排序（与 `buildCompareMatrix` 一致）。某配方没有该子件时单元格显示 `—`。
- 左侧固定列：子件编码 / 子件名称 / 规格型号（可关）/ 单位，`position:sticky` 吸左。

### 要素列组开关

沿用现有「视图」下拉的交互和 localStorage 记忆方式，**按组勾选**而不是逐列：

| 组 | 包含列 | 默认 |
|---|---|---|
| 原配方 | 数量、比例 | 关 |
| 系统 | 系统单价、系统分价 | 关 |
| 当下 | 当下价格、当下分价 | **开** |
| 模拟 | 模拟数量、模拟比例、模拟分价 | 关 |
| 规格列 | 规格型号（左侧固定区） | 开 |

默认只开「当下」组。3 本配方全开是 27 列——靠默认收窄 + 左侧列吸附 + 横向滚动兜底。

新 localStorage key：`formula_cost_view_options`（与对比面板的 `formula_display_options` 分开，两块视图语义不同）。

### 对比模式下的编辑权限

`state.manualPrices` 和 `state.simulatedQuantities` 现在都是**按子件编码全局存**的，不分配方。

- **「当下价格」可编辑**：改一次所有列同步，语义正确（同一个原料同一个价）。
- **「模拟数量」只读**：它同样是全局的，但各版本的原数量不同，多列并排会出现「同一个模拟数量、三个不同比例」的自相矛盾画面。表头加提示「模拟数量请回单本模式编辑」。
- **「模拟销售价格 / 其他综合成本」可编辑**：本来就是按配方存的，每列独立。

### 状态

```js
state.costMode = 'single' | 'compare'
state.costSelectedKeys = new Set()          // recipe.key
state.costView = { recipeGroup:false, systemGroup:false, currentGroup:true, simGroup:false, spec:true }
```

`costSelectedKeys` 不持久化（跟着查询走）；`costMode` 和 `costView` 持久化。

### 不在本次范围

「导出核算Excel」不覆盖对比模式。现有 `save_recipe_cost_workbook()` 是每本配方一个 sheet，要出对比版 sheet 是独立一轮的工作量。对比模式下该按钮保持现有行为（导出全部配方，每本一 sheet）。

### 改动文件

`services/public-web/formula/index.html`、新增 `services/public-web/formula/cost-core.js`、`tests/test_formula_frontend.py`。

---

## 新模块：`cost-core.js`

`index.html` 已 820 行 / 73KB，成本对比再往里塞会更难改。按 PR#185 抽 `compare-core.js` 的同一套「页面薄壳化」做法，把纯计算抽出来：

```
services/public-web/formula/cost-core.js
├─ recomputeRecipeCosts(recipe, manualPrices, simulatedQuantities)  // 从 index.html 平移
├─ buildCostMatrix(recipes, selectedKeys)                            // 子件并集 + 排序
├─ profitMetrics({ salesPrice, cost, otherCost })                    // -> { margin, markup }
└─ profitTooltipText({ ... })                                        // 代入数字的算式文本
```

约束与 `compare-core.js` 一致：零 DOM、零存储、零网络，UMD 包装。四个纯函数可用 `node --test` 直接跑。

`compare-core.js` **不改**——它是网站与小程序共享的权威源，改动要同步小程序仓；成本核算不在小程序里（RBAC 门控 `formula.cost.calculate`），没有共享需求。

## 验证

| 层 | 命令 | 覆盖 |
|---|---|---|
| 后端 | `pytest tests/test_recipe_query.py` | 变更 1 的四组用例（含反证） |
| 前端契约 | `pytest tests/test_formula_frontend.py` | 新标签、新 key、新列组开关、tooltip 类名 |
| 纯算法 | `node --test`（cost-core.js） | 利润两口径、边界（售价 0 / 成本 0 / 售价缺失）、矩阵并集与排序 |
| 端到端 | 部署后拿含色粉包的那本 BOM 手工复核（编码见运维记录，不落公开仓） | 分母不含「份」、色粉包比例空、利润数字与手算一致 |

部署走 `AliECS/docs/runbooks/deploy.md`；`public-web` 是静态页，`backend-api` 改了 `bom_query.py` 需要重建镜像。
