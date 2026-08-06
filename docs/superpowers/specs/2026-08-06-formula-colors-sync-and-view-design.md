# 标准型号色彩空间：物料清单补建 + 标签与设置重组（设计）

2026-08-06 定稿。对应页面 `/formula/colors/`。

## 现状

| 关注点 | 位置 | 现状 |
|---|---|---|
| 页面 | `services/public-web/formula/colors/index.html` | 单文件 657 行，three.js + camera-controls |
| 只读接口 | `services/backend-api/app/routers/formula_colors.py` | 读 `external_records` + `tplus_bom_records`，只返回 Lab 完整的行 |
| 反向写回 | `services/doc-sync-worker/app/pipelines/tplus_parent_match.py` | 每天全量同步后随 `run-loop` 跑一次，**只更新已有行，不新增行** |

三项待办：

1. 企微「标准型号0117」缺 T+ 物料清单的行，需要自动补建，人工后续补标准。
2. 型号详情的字段勾选只作用于选中的那一个色点，且刷新后丢失。
3. 「视图设置」浮层压在三维画布左下角，与顶部的数据源/视图按钮割裂。

拆两个 PR，互不阻塞：PR-A 在 doc-sync-worker，PR-B 在 public-web。

---

## PR-A：企微表自动补建物料清单行

### 目标

T+ 当前有效的**全部**父件，在企微「标准型号0117 / 标准型号规格&月统计」中都有一行，带「父件编码」和「父件名称」；Lab、容差、型号等列留空等人工补。

### 改动

全部集中在 `tplus_parent_match.py`：

1. **新增补建阶段**，跑在现有 `plan_updates` 之后：

   ```
   待建编码 = load_active_bom().keys() − {已有行的「父件编码」非空值}
   ```

   用已存在的 `client.add_records()` 分批（200/批）写入。新行只写 4 列：

   | 列 | 值 |
   |---|---|
   | 父件编码 | T+ 的 `Code` |
   | 父件名称 | T+ 的 `Name` |
   | T+匹配状态 | `一致`（名称直接取自 T+，天然一致） |
   | T+核对时间 | 本轮 `checked_at` |

   「型号」及所有 Lab / 容差 / ΔE 列**一律不写**。人工按「型号为空」即可筛出待补标准的行——不额外加标记列。

2. **核对时间改为按需写**。现在 `plan_updates` 对每一行无条件塞 `F_CHECKED_AT`，表从 41 行涨到全量后，每天都要重写整表且无信息量。改成：仅当该行的父件名称或匹配状态实际发生变化时才带上核对时间。

3. **告警文案扩展**：`build_alert` 增加「🆕 本轮补建 N 行」及前 20 个编码，仍推 `TPLUS_PARENT_MATCH_CHAT_ID`。

### 已确认的行为边界

- **人工删除的行，下一轮会被重新建出来**。已与用户确认接受，首版不做删除白名单。
- 父件编码仍是执行主键，补建**只新增**、绝不改写已有行的编码——沿用现有红线。
- `tplus_bom_records` 必须带 `missing_since IS NULL` 过滤，否则会把已作废版本的旧名称建进表。已有逻辑复用，不重写 SQL。
- `load_active_bom()` 返回空时整个管道跳过（现有保护），补建阶段同样受该保护覆盖——避免 T+ 侧异常时把表清空/误建。

### 落地顺序

先 `python -m app.main tplus-parent-match --dry-run`，打印待建行数与前若干编码，确认量级后再真写。dry-run 不得调用 `add_records`。

### 影响面

- 企微表行数从 41 涨到 T+ 全量父件数。doc-sync 全量同步该表的耗时随之增长。
- `/v1/formula/colors` 的 `meta.total_records` 会同步变大，但 `items` 只含 Lab 完整的行，**三维图不受影响**，页面 badge 用的是过滤后的 `enriched.length`。

### 验证

- `python -m unittest tests/test_tplus_parent_match.py`，补两个用例：待建集合计算正确；dry-run 不触发 `add_records`。
- 生产先 dry-run 看数，再实跑，最后到企微表核对新行的 4 列取值与空列。

---

## PR-B：标签层与设置重组

### B1 全部显示标签（借鉴 obsidian-3d-graph）

参考 `HananoshikaYomaru/obsidian-3d-graph` 的 `ForceGraph.ts` / `ForceGraphEngine.ts`。

**渲染方式改为 DOM 覆盖层。** 现有 `makeTextSprite` 每块标签建 192×72 canvas + texture，几百块会吃爆显存——这是原先要限制点数的唯一原因。改成一层 `position:absolute; pointer-events:none` 的 div 容器叠在 canvas 上，每个可见点一个 div，每帧用 `positionFor(item).project(camera)` 投影成屏幕坐标写 `transform` 和 `opacity`。该投影计算 `hitAt()` 中已有同款，不引新依赖，**不用 CSS2DRenderer**（我们的色点是 `InstancedMesh`，没有独立 Object3D 可挂，套 CSS2D 反而要额外维护映射）。

**距离淡化用余弦缓动**，抄 `ForceGraphEngine.ts:295-307`：

```js
const distance = worldPos.distanceTo(controls.getTarget(_v3));
const normalized = Math.min(distance, focal) / focal;
const eased = 0.5 - 0.5 * Math.cos(normalized * Math.PI);
const base = 1 - eased;
```

焦点直接取 `controls.getTarget()`——camera-controls 自带，且 `focusSelected` / `fitToBox` 之后它正好是用户的关注点，比原项目每帧跟随相机造隐形 `myCube` 更贴合本页交互。

**三态 opacity**，抄 nodeThreeObject 内的分支：

| 情形 | opacity |
|---|---|
| 该点是 hover 目标或当前选中 | `1` |
| 有高亮集且该点不在集内 | `clamp(base, 0, 0.2)` |
| 其余 | `base` |

标签样式固定深色半透明底板 + 圆角 padding（原项目 `rgba(0,0,0,.5)`）。本页色点是真实颜色、深浅都有，没有底板时浅色点上的文字读不出来，这条必须照抄。

**行为**：

- 悬浮 tooltip、点击选中标签的现有行为保持不变。
- 新增开关「全部显示标签」，默认关闭。开启后当前筛选出的所有色点各挂一块标签，内容由型号详情里已有的 `data-label-field` 勾选决定，勾选一变全部实时重建——这就是「所有色点的显示都刷新」。
- 取消原定的 60 点阈值。仍受现有 `MAX_POINTS=512` 约束。
- 节流：opacity 只随相机变化，相机静止时（`controls.active === false`）跳过整轮 DOM 写入。
- 标签互相遮挡不做碰撞剔除，靠距离淡化缓解——与原项目一致。

**淡化距离滑块**：`focal` 做成设置项，默认 `12`（全局视图数据包围盒量级；Δ 视图 reach = `DELTA_RANGE * DELTA_SCALE` = 11.5），滑块范围 2–60，拉到最大约等于不淡化。默认值上线后按实际观感微调常量。

### B2 设为默认

「设为默认」按钮把三项写入 `localStorage['aliecs_formula_colors_view_prefs']`：`labelFields`（数组）、`showAllLabels`（布尔）、`labelFocalDistance`（数字）。页面初始化时读取并套用，旁边配「恢复默认」清除该键。

**只持久化这三项**，其余视图设置（容差盒开关、参考色域、放大倍数等）不进 localStorage——避免下次打开时容差盒莫名其妙是隐藏的。

### B3 设置收纳到顶部

- 删除画布左下角的 `.view-settings` 浮层（`<details id="viewSettings">`），三维画布上不再压任何浮层。
- 顶部 `.mode-row` 改为 `[标准型号][参考示例][Δ 判色视图] ｜ [⚙ 显示设置]`。
- 点齿轮在工具栏下方展开一条横幅面板，**挤压画布高度而非遮挡**：面板作为 `.page`（flex column）的一个新节点插在 `.toolbar` 与 `.workspace` 之间，`.workspace` 的 `flex:1` 自动让出高度。面板分 5 组：

  | 分组 | 内容 |
  |---|---|
  | 视角 | 复位 / a\*b\* 俯视 / L\*–a\* 立面 / L\*–b\* 立面 / 聚焦选中 / 单指平移 |
  | 显示 | 容差盒 / 标准色点 / 参考色域 三个开关 |
  | 容差盒 | 放大倍数 / 透明度 / 棱线 / 只看容差重叠 |
  | 参考色域 | 模式 / 显示方式 / 透明度 / L\* 水平切面 / 切面 L\* |
  | 标签 | 全部显示标签 / 淡化距离 / 设为默认 / 恢复默认 |

- 移动端：面板默认收起；展开时画布收缩，仍满足「工具页移动端一屏免滚动」的既有红线，改完用 playwright 量页面高度取证。
- 元素 id 全部沿用，只改 DOM 位置和容器，避免 657 行脚本里的 `$('...')` 大面积改写。

### B4 hover 联动高亮重叠型号

原项目 hover 节点时高亮邻居、压暗其余。对应到判色场景：hover 或选中某型号时，把**容差盒与它重叠**的型号计入高亮集，其余进入 `clamp(base, 0, 0.2)` 的压暗态。

复用现有 `boxesOverlap()`；重叠判定必须走真实比例（`toleranceRange` 的 `magnify` 默认 1），不能用放大后的盒，否则结论失真——这条现有注释已写明，沿用。

高亮集同时作用于标签 opacity 和容差盒 opacity。这让「只看容差重叠」那个二元筛选之外，多一条不改筛选就能看清重叠关系的路径。

### B5 搜索命中自动聚焦

`#formulaSearch` 现在只做筛选。改为：筛选后若结果**恰好唯一**，自动 `selectPoint` 并 `focusSelected()` 飞过去；结果多于一个时维持现状不动相机。

### 验证

- 打开页面确认无 JS 报错，核心入口 smoke：切数据源能触发网络请求。
- 开「全部显示标签」，转动相机确认远处标签淡出、hover 目标全亮、无关型号压到 20% 以下。
- 勾选/取消详情字段，确认所有标签同步刷新。
- 点「设为默认」后刷新页面，确认勾选与开关被还原；点「恢复默认」后再刷新，确认回到出厂状态。
- 搜到唯一型号时相机自动飞过去。
- playwright 量移动端视口下页面高度，确认面板展开与收起都不产生纵向滚动。

---

## 风险与回退

| 风险 | 处置 |
|---|---|
| 补建行数远超预期，把企微表撑到人工不可用 | dry-run 先报数，用户确认后才实跑 |
| 补建后 doc-sync 全量同步该表变慢 | 观察一轮同步耗时；必要时该表转增量 |
| 512 个 DOM 标签导致掉帧 | 相机静止时跳过更新；仍不够则按可见点数降级为只显示选中项 |
| 设置面板挤压画布后移动端超一屏 | playwright 取证，不达标则改回抽屉式 |

回退：PR-A 与 PR-B 独立，各自 `git revert` 即可。PR-A 已补建的企微行不会随 revert 消失，需人工删（或保留——空行不影响页面）。
