# Formula（系统配方 / 成本核算）Runbook

页面：`https://hydwang.xyz/formula/`。出问题先读本文件，再动代码。
接口契约与数据来源见 `docs/recipe-query.md`（英文，讲"是什么"）；本文件讲"踩过什么坑"。

## 代码入口

| 关注点 | 文件 |
|---|---|
| 页面全部渲染与交互 | `services/public-web/formula/index.html`（单文件，含内联 JS/CSS） |
| 对比矩阵、行排序、列排序、视图开关 | `services/public-web/formula/compare-core.js` |
| 利润口径、成本矩阵、本地重算 | `services/public-web/formula/cost-core.js` |
| 查询/反查/成本核算接口 | `services/backend-api/app/routers/recipes.py` |
| BOM 解析、匹配、成本计算 | `services/backend-api/app/recipes/bom_query.py` |
| 对比表 Excel 导出 | `services/backend-api/app/recipes/compare_export.py` |

⚠️ `compare-core.js` 同时是微信小程序共享模块的**权威源**（文件头有说明）。改完它，weapp 仓要跑 `node scripts/sync-shared.mjs` 同步，否则两端算法会漂。

## 按子件反查配方（2026-08-24 上线）

流程是**两段式**，不是一次查询：`POST /v1/recipes/children/search` 罗列候选子件 → 用户勾选确认 → `POST /v1/recipes/query` 带 `child_codes` 反查。

- **`child_codes` 走精确匹配，不能改成子串匹配。** 父件查询用的 `_query_match_mask` 是 `str.contains`，`HYD-419` 会连带命中 `HYD-4197`；反查如果复用它，会多出用户没勾的配方。同理不能"把反查出的父件编码塞回 `query` 文本"——`query` 还有 `max_length=100`，几十个父件直接超长。
- **命中的是配方，不是行。** `_filter_by_child_codes` 先定位含这些子件的**配方版本**（父件编码 + 版本号），再把明细收敛到这些版本；同一本配方里没命中的子件也必须在结果里，否则对比和成本核算都是残的。
- **`child_match=all`（全部包含）对同类子件必然为空。** 实测：关键字「硬脂酸」只有两个候选——硬脂酸锌（166 本）、硬脂酸钙（5 本），它们**互为替代品**，没有任何一本配方同时含两者，`all` 返回 0 本。`all` 只对跨类组合有意义。0 命中时页面必须说清原因，静默出一个空面板会被当成功能坏了。
- **反查参数必须进 `_remember_recipe_query` 的上下文**，否则延迟生成的导出文件会退化成"按关键字查父件"，下载下来是空表。
- 候选上限 `CHILD_SEARCH_LIMIT = 200`，超出返回 `truncated=true`，页面提示细化关键字。

**前端的单一来源**：`state.queryScope = {query, child_codes, child_match}`。反查模式下输入框里是**候选关键字**而不是配方编码——查询、成本核算、Excel 导出如果各自去读 `queryInput.value`，`child_codes` 必漏。新增任何用到"当前查了什么"的地方，都从 `queryScope` 取。

## 成本核算的利润口径

- 售价**统一取「模拟销售价格」**（用户没手填则回落系统销售价格），三个成本口径（系统合价 / 当前成本合价 / 模拟合价）只换成本、不换售价。
- 每个口径下三个指标：利润额 = 售价 − 合价 − 其他综合成本；毛利率 = 利润额 ÷ 售价；成本加成率 = 利润额 ÷ 合价。
- 三个口径的定义只在 `COST_CALIBERS` 写一次，单本卡片和多选对比汇总表都从这里取。两处各写一份，标签、顺序、取的字段任意一处不一致，同一个数字就会有两种叫法。
- 合价只含进比例分母的原料（每 kg）。包装袋、色粉包等不计比例的子件不在内，要计入「其他综合成本」。

## 视图开关与列排序

- 视图开关的默认值在 `compare-core.js` 的 `VIEW_DEFAULTS`，`loadViewOptions()` 会用它兜底老 localStorage，新增键无需迁移。
- **列序（哪本配方排在前面）的单一来源是页面的 `selectedVersions()`**，Excel 导出走 `buildComparePayload({colSort})`。任何地方绕过去自己排，表头和数据格就会错位。
- 列排序以某一子件行为基准，按该行的比例或数量排；**不含该子件、或该口径为 0 的配方恒定压在最后**，升降序都不把它们翻上来（与行排序按比例的处理同判据）。
- 换查询后要清掉 `colSort.itemCode`：那个子件在新配方里多半不存在，留着会让人以为排序坏了。

## Excel 导出的列号

`compare_export.py` 的信息列（状态/子件编码/子件名称/规格型号/单位）**列号必须由开关算出**（`col_code` / `col_name` / `col_spec` / `col_unit` / `left_cols`），不能写死 2/3/4。任一列可隐藏，写死会让后面所有列——包括左对齐判据——整体错位。

## 排障

**「多选对比」的快捷选择点了没反应**：`renderCost()` 里的"默认全选"必须只在**进对比模式**和**换查询**时各做一次。放在每次渲染里，它区分不了"刚进入模式"和"用户主动全不选"，会把清空当场填回去。页面上的「已选 N / M 本」计数是用来区分"没反应"和"没变化"的，别删。

**浏览器报 `Failed to fetch` 但服务端什么都没有**：先 `sudo grep recipes /var/log/nginx/access.log`。**没有记录就是请求没到服务器**，根因在客户端侧，不要在服务端接着猜。已核对过的服务端事实（2026-08-24）：`client_max_body_size 25m`、`proxy_read_timeout 3600s`、最重的反查（270 本配方）query 0.02s / cost 0.30s / JSON 1.0MB，全部 200 无 5xx。⚠️ 该现象截至 2026-08-24 仍未定案，缺浏览器侧证据（DevTools Network 勾 Preserve log + Console 完整报错）。

**前端改动怎么验**：不必靠真机试错，也不需要登录态。把探针脚本追加到 `index.html` 的副本（如 `_probe.html`，放同目录才能加载 `compare-core.js`），在 `load` 事件里直接调页面的全局函数（内联脚本里的函数声明都是全局的）、伪造 `state`、把结果写进 `document.body.dataset.probe`，然后：

```bash
python -m http.server 8791          # 在 services/public-web 下
chrome --headless --disable-gpu --window-size=1600,1000 \
       --virtual-time-budget=6000 --dump-dom http://localhost:8791/formula/_probe.html
```

读回 `data-probe` 即可。**探针文件用完必须删掉**，别提交进仓库。

## 标准型号色彩空间与图片拍照取色（/formula/colors/）

入口：`/formula/colors/`。代码单文件位于 `services/public-web/formula/colors/index.html`，设计详见 [`docs/superpowers/specs/2026-09-23-formula-colors-image-picker-design.md`](../superpowers/specs/2026-09-23-formula-colors-image-picker-design.md)。

- **图片取色工作台（`#pickerModal`）**：支持手机相机拍照直传、本地相册、拖拽与剪贴板 `Ctrl+V`；同时内置 3 块经典塑胶样板预设（红光面、绿咬花、黄哑面）便于无图时快速体验。
- **手势与防遮挡放大镜**：单指/鼠标拖动准星定位，双指捏合/滚轮无级缩放全图；触控时上方自动浮现 **$4\times$ 悬浮圆形放大镜（`#loupe`）**，彻底消除移动端手指遮挡取样圆圈的痛点。
- **1:1 双拼大色块比对（肉眼校色核心）**：
  - 左半边：直接截取准星下的原图高倍微观实况（保留光泽、颗粒纹理）；
  - 右半边：展示算法提取出的配方目标纯色；
  - 配备 **⚪ 白底 / 🔘 50% 中性灰（D65 推荐） / ⚫ 黑底** 切换，消除环境对比错觉。
- **实物材质滤波算法（4 种模式）**：
  - `trim-glare`（光面塑料首选 ⭐）：截除前 15% 最亮高光耀斑像素，防止提取色泛白；
  - `trim-shadow`（皮纹/咬花首选）：截除后 15% 最暗凹陷阴影像素，防止提取色偏暗偏脏；
  - `average`（平整哑光表面）：物理线性 RGB 均匀积分；
  - `median`（抗噪点/杂质）：取亮度中位数稳态色。
  - ⚠️ **色彩学踩坑**：像素平均必须在**物理线性 RGB 空间**进行（先去 Gamma 后平均），若直接在 sRGB 字节上取代数平均会导致提取出来的颜色明度 $L^*$ 显著偏暗。
- **系统查找闭环**：准星移动时毫秒级预演 Top 3 近邻型号（含 `✅容差内` 标签）；点击应用自动回填主界面的 `#targetL`、`#targetA`、`#targetB` 并调用 `locateTarget`，在三维空间中高亮定位并拉出引线。
- **前端契约验证**：
  ```bash
  python3 -m unittest tests/test_formula_color_space_frontend.py
  ```

## 热更新

`public-web` 是 nginx 静态；热更新前从 [fleet](../fleet.md) 核对目标和容器，从 [deploy](deploy.md) 读取发布、重建与回灌要求。操作须在用户授权范围内；涉及共享脚本时同时发布依赖。

自证判据要**双向**：新判据命中数 > 0 **且**旧判据反证 = 0，同时打印 `%{http_code}` 和 `size_download`——只看一个计数分不清"没生效"和"没测到"。服务端自证只能证明"我返回了什么"，用户看到什么另说：让人验证前第一句写「用无痕窗口打开」。
