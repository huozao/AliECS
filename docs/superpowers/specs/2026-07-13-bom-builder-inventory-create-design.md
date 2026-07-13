# bom-builder 完善：新建存货流程、建议编码与查重、响应式（2026-07-13）

## 背景与问题

用户在 https://hydwang.xyz/bom-builder/ 反馈三个问题：

1. 新建父件编码后提交，报"T+ BOM 写入功能尚未启用"。
   诊断结论：生产 `release-meta.env` 于 07-12 被 sops 渲染重写时丢失
   `TPLUS_BOM_WRITE_ENABLED`，deploy.sh 默认回落 `false`。与新建/已有父件无关，
   所有提交路径都被拦。属环境修复，不属本 spec 代码范围（见"前置修复"）。
2. 新建父件实质是在 T+ 新建存货，但界面没有暴露存货应有的选项组
   （基本信息 / 计量单位 / 存货属性），属性写死在后端。
3. 没有"建议编码"，手输编码重复只有 T+ 侧报错且报错未必透传完整。

## 前置修复（部署环境，随实施一起做，需用户授权）

- devbox `sops set` 在 `infra/secrets` 的 aliecs.enc.env 增加
  `TPLUS_BOM_WRITE_ENABLED=true` → render → 重建 `ecs-backend-api-1` 与
  `ecs-tplus-write-worker-1`。sops 源才是持久层；只改 release-meta.env 会再次被渲染覆盖。

## 已确认的事实（实测 T+ 生产数据）

- 存货属性字段名：外购=`IsPurchase`、销售=`IsSale`、自制=`IsMadeSelf`、
  生产耗用=`IsMaterial`、委外=`IsMadeRequest`、虚拟件=`IsPhantom`。
  以现存 `30122027-3027` 验证：前 5 项 True，`IsPhantom` False。
- 存货分类（末级）：00 未分类 / 01 原材料 / 02 色粉 / 03 辅材 / 04 成品库 /
  05 五金建材 / 06 物料清单 / 07 废料库 / 09 半成品 / 10 助剂包 / 11 色粉包 /
  12 代加工（末级 1201 成品、1202 代加工材料）。
- 历史编码与分类前缀大量不一致（如物料清单类存在 `0316-CO712`、`旧2042`）。
  用户拍板：**历史不用管**，新建议编码一律 = 分类编码前 2 位 + 不重复流水。
- 每日 02:00 同步的存货导出 Excel（`/app/tplus-output/excel/inventory_*.xlsx`）
  有 `Code` 列但 `InventoryClass` 列为空；建议编码/查重只依赖 `Code` 列，不受影响。

## 用户拍板的决策

1. 数据源选 **A：纯本地导出**。建议编码与前端查重都读每日导出；
   当天新建的重复靠提交时 T+ 拒绝兜底（T+ 会禁止重复创建），届时人工改流水号。
2. 存货属性默认勾选 **5 项**：外购、销售、自制、生产耗用、委外；**虚拟件默认不勾**。
3. 所属类别默认"物料清单"，计量单位默认"kg"（沿用现状常量）。

## 设计

### 后端（services/backend-api）

**新端点 `GET /v1/tplus/inventory-code-suggestion?class_code=06`**
（router：`app/routers/tplus_bom.py`，权限 `tplus.bom.write`）

- 取 `class_code` 前 2 位作前缀 `PP`。
- 读最新存货导出的 `Code` 列，收集匹配 `^PP\d{6}$` 的编码，取流水最大值 +1；
  无匹配则从 `PP0000` 起 → 首个建议 `PP000001`（6 位流水，对齐现有 `01000009` 的 2+6 格式）。
- 返回 `{"suggested": "06000001", "prefix": "06", "source_file": "..."}`。
- 流水耗尽（>999999）返回 409，提示人工定编码（实际不会发生，防御性）。

**编码查重：复用现有 `GET /v1/tplus/inventories?q=<code>`**

- 不新增端点。前端拿搜索结果做**精确等值比对**判断重复。
- 现端点已过滤停用存货；查重不应漏掉停用编码（T+ 对停用编码同样判重）——
  给端点加参数 `include_disabled=true`（默认 false 保持现行为），查重调用时带上。

**存货属性进入领域模型（`app/tplus_bom.py` + `app/routers/tplus_bom.py`）**

- `BomParent`（及其子类 `BomChild`）新增字段：
  `is_purchase / is_sale / is_made_self / is_material / is_made_request / is_phantom`，
  均 `bool`，默认值 = 父件默认（前 5 真、虚拟件假）。仅 `source=custom` 时生效。
- `build_inventory_create_payload` 改为读取这些字段填充
  `IsPurchase / IsSale / IsMadeSelf / IsMaterial / IsMadeRequest / IsPhantom`，
  删除按 `kind` 写死的逻辑；`kind` 参数保留仅用于报错文案。
- 校验：6 项不能全为假（T+ 不接受无属性存货——实施时以 T+ 实测为准，若 T+ 接受则放开）。
- 兼容：`build_inventory_create_payload` 在 item dict **缺属性键**时回退旧版按
  `kind` 写死的逻辑（parent=销售+自制，material=外购+生产耗用），保证旧草稿行为不变；
  带属性键（新前端提交）时按用户勾选。pydantic 模型字段设为可空而非硬默认，
  以便区分"未提供"与"显式关闭"。

**T+ 报错透传（`services/tplus-sync-worker`）**

- 核查 `ChanjetClient.post` 对 T+ 业务失败（HTTP 200 但 body 带错误、或非 200）的处理，
  确保原始错误文本（如"存货编号：30122027-3027不唯一，请尝试修改该编号中的流水号后再操作"）
  完整进入 `tplus_bom_submissions.error_json.message`，不截断、不改写。
- 前端提交状态卡已渲染 `error.message`，透传打通后无需额外前端逻辑。

### 前端（services/public-web/bom-builder/index.html）

**新建父件区重组为三个分组**（桌面平铺、手机可折叠 `<details>`，"基本信息"默认展开）：

1. **基本信息**：父件编码、父件名称、规格型号、所属类别（默认物料清单）。
2. **计量单位**：单位下拉（默认 kg）。
3. **存货属性**：6 个复选框，默认勾选外购/销售/自制/生产耗用/委外，虚拟件不勾。

**建议编码提示条**（编码输入框下方）：

- 选定/切换所属类别后调用 suggestion 端点，显示
  `建议编码：06000001（点击填入）`；点击即填入编码框。
- 编码框失焦（或输入停顿 400ms）后用 `q=<code>&include_disabled=true` 查重，
  精确匹配到即红字提示 `编码已存在：<名称>`，并在本地提交检查（localCheck）里阻断提交。
- 建议编码与查重均为辅助：接口失败不阻塞录入，只提示"查重不可用，提交时以 T+ 校验为准"。

**新增原料弹窗**同步升级：

- 同样的建议编码条（默认类别"原材料"→ 建议 `01xxxxxx`）与失焦查重。
- 加存货属性复选框组，默认勾选：外购 + 生产耗用（沿用旧版原料写死值），其余可手动勾。

**响应式**（对齐首页 PR#182 的思路）：

- ≤900px：维持现单列布局。
- >900px：`.wrap` 放宽至 ~1100px，父件+选项在左列、子件表格在右列（CSS grid 两栏），
  减少滚动；sticky 底栏不变。

**提交反馈**：

- 提交状态卡展示 T+ 错误原文全文（`white-space:pre-wrap` 已具备）。
- 队列事件（inventory_created / inventory_reused 等）已存在于 events，
  状态卡追加一行摘要展示（如"已在 T+ 创建存货 06000001"），方便确认新建存货成功。

### 数据流

```
选类别 → GET inventory-code-suggestion → 点击填入编码
手输编码 → GET inventories?q=&include_disabled=true → 精确匹配 → 红字警告+阻断
提交 → draft validate（服务端校验）→ confirm → submit（写入开关）
    → write-worker：查 T+ 是否已存在（同名同单位则复用，否则报错）
    → inventory/Create（T+ 拒绝重复 → 错误原文入 error_json）
    → bom/Create → 写后查询验证
前端轮询 submission → 展示状态 + T+ 错误原文 + 存货创建事件摘要
```

### 错误处理

- suggestion / 查重接口 404（导出未同步）或 5xx：前端仅提示不可用，不阻塞录入。
- 手输重复编码：前端阻断提交（本地导出可见的重复）；
  当天新建导致的漏网重复：worker 侧 T+ 报错，原文透传到状态卡，用户改流水号后重提。
- 属性全不勾：前端本地校验 + 服务端 validate 双重拦截。

### 测试

- 后端单测（`tests/`）：suggestion 端点（空前缀首建 / 已有取最大 +1 / 非法 class_code）、
  `include_disabled` 行为、属性字段进 payload（默认值 / 显式关闭 / 全假拒绝）、旧草稿兼容。
- worker 单测：ChanjetClient 错误文本透传进 error_json。
- 前端静态冒烟：沿用双端口 stub + Playwright `channel=chrome`（勿下载浏览器），
  覆盖：建议编码点击填入、重复编码红字与阻断、属性默认勾选状态、桌面两栏/手机单列渲染。

## 范围外

- 历史编码清理/迁移（用户明确不管）。
- 存货档案导出补 `InventoryClass` 列（另行处理，本设计不依赖）。
- BOM 修改/删除（页面仍只做新建）。
