# bom-builder BOM 审核功能设计（2026-07-14）

## 背景

bom-builder 现已能新建父件存货并把 BOM 写入畅捷通 T+（PR#186/#187 上线，写入链路已真机首单成功，BOM 落库为 `VoucherState=未审(Code=00)`）。下一步需要在同一入口对提交的 BOM 做**审核**，且必须以 T+ 真实状态为准，杜绝"假装提交/假装审核"。

## 已确认的 T+ 事实（本次探测）

- `bom/Query`（`{"dto":{"Code":..,"Version":..}}`）返回单条 BOM，含 `VoucherState`；未审=`{"Code":"00","Name":"未审"}`。此字段是"是否待审"的**权威判据**。
- `bom/Audit` / `bom/UnAudit` 是真实服务端点（dto 形态返回业务提示"未查找到物料清单"，非路由 404），页内直接审核技术可行。
- 精确的 Audit 参数形态（dto 里 `ID` vs `Code+Version`）**未敲定**——探测时 T+ 触发限流 `EXSV0011`。实现第一步在限流恢复后用测试 BOM（如 267）探成功格式再写死。
- `bom/Query` 列表分页形态（`{"param":{"PageSize","PageIndex","SelectFields"}}`）本次被限流未验证成功——实现时需确认它支持列表拉取，用于 `scope=all`。

## 用户拍板的决策

1. **列表范围**：默认「本工具建的未审」，可切换到「T+ 全部未审」。
2. **位置**：bom-builder 同页顶部加 `录入 | 审核` 两个 Tab；提交成功自动跳「审核」Tab 并刷新。
3. **审核粒度**：每行一个「审核」按钮 + 二次确认；**审核前可行内展开看子件明细**（看清配方再审）。批量审核列为范围外。

## 交互设计

### 页面结构

- 顶部 topbar（复用现有 SSO 登录/退出）下加 Tab 切换：`录入`（现有录入表单全部）/ `审核`（新）。
- Tab 状态存内存即可（切换不丢录入草稿——录入态本就有 localStorage 自动保存）。
- 提交成功（`pollSubmission` 到 success）后自动 `switchTab('audit')` 并触发列表刷新。

### 审核 Tab

- 顶部工具条：范围切换 `本工具建的 | T+ 全部未审`（radio/segmented）+「刷新」按钮 + 「同步于 HH:MM:SS」时间戳。
- 列表每行：父件编码 · 名称 · 版本号 · 生产数量 · 状态徽章（未审）· 展开箭头 · 「审核」按钮。
- 展开箭头 → 行内展开该 BOM 子件明细（编码/名称/单位/需用数量），数据来自该行 `bom/Query` 结果（列表接口已带子件则直接用，否则点击展开时按需查一次）。
- 「审核」→ 二次确认弹窗（列父件 编码+名称、版本、子件条数）→ 调后端审核 → 成功该行淡出移除；失败错误原文留在该行。
- 空列表提示「当前没有待审核 BOM」。

### 防假装三条硬保证

1. **列表 = T+ 实时查**，非查本地 `tplus_bom_submissions`：
   - `mine`：读本地 success 提交的 (code, version) 集合，逐条 `bom/Query` 取 `VoucherState`，仅保留未审。
   - `all`：`bom/Query` 列表分页拉全部，服务端过滤未审。
2. **审核 = 真调 `bom/Audit` + 立即复查**：调用后再 `bom/Query` 确认 `VoucherState` 翻成已审，只有复查确认才算成功、才移除该行；失败或未翻则报错留列表。
3. **刷新 = 重新实时拉**：审核成功自动重拉 + 手动刷新按钮；列表所见即 T+ 真实状态。

## 后端设计（services/backend-api）

`app/routers/tplus_bom.py` 新增两个**同步**端点（审核是单次快操作，不入 write-worker 队列；队列为多步高危 create 而设）。查询走现有 `_chanjet_read_post` 模式，写走同款带 open_token 的 POST。

### `GET /v1/tplus/bom-pending?scope=mine|all`

- 权限 `tplus.bom.audit`（见下）。
- `scope=mine`：查 `tplus_bom_submissions` 中 `status='success'` 且 `result_bom_id` 非空的记录，取其 parent code+version（从 `request_json.bom.dto` 或提交时存的字段），逐条 `bom/Query`，仅回 `VoucherState.Code='00'` 的。
- `scope=all`：`bom/Query` 列表分页（`param` 形态，`SelectFields` 含 Code/Version/Inventory/VoucherState/子件），服务端过滤未审。
- 返回：`{"items":[{code,name,version,produce_quantity,voucher_state,bom_id,children:[{code,name,unit_name,required_quantity}]}], "synced_at":...}`。
- 错误：T+ 查询凭据缺=503；限流/网络=502（前端提示可刷新重试）。

### `POST /v1/tplus/bom-audit`

- 权限 `tplus.bom.audit`。
- body `{code, version, bom_id?}`（业务键为主，bom_id 兜底）。
- 调 T+ `bom/Audit`（参数形态实现时探定），随即 `bom/Query` 复查 `VoucherState`。
- 返回 `{"audited": bool, "voucher_state": {...}}`；未翻成已审 `audited=false` 带 T+ 原文（沿用 business_message 透传思路）。
- 成功 `_audit(actor, "tplus.bom.audit", "tplus_bom", code+version)`。

### 权限

新增 `tplus.bom.audit`，DB 迁移里默认授给 admin 角色 + 当前操作用户（与 `tplus.bom.write` 分离，为将来"提交人≠审核人"留口；当前一人操作等于全放开）。

## 前端设计（services/public-web/bom-builder/index.html）

- 顶部 Tab 结构 + `switchTab()`；录入区整体包一层 `#entryTab`，审核区 `#auditTab`。
- 审核区：范围 radio、刷新按钮、时间戳、列表容器 `#auditList`、行模板（展开/审核）。
- JS：`loadPending(scope)` 调 `bom-pending`；`toggleDetail(row)` 展开子件；`auditRow(code,version)` 确认→调 `bom-audit`→成功淡出+重拉。
- 复用现有 `api()`/`token()`/SSO/`esc()`。审核相关 JS 与录入 JS 同文件但函数分组清晰。
- 接口失败只提示不崩：列表加载失败显示「加载失败，点刷新重试」。

## 错误处理

- `bom-pending` T+ 限流/网络失败：前端提示可刷新；不影响录入 Tab。
- `bom-audit` T+ 拒绝或复查未翻：`audited=false` + 原文，行保留并标红，用户可重试或去 T+ 查。
- 审核后复查若 T+ 短暂未同步：以复查结果为准，未翻就不移除（宁可让用户再刷新，不假装成功）。

## 测试

- 后端单测（mock T+ 查询/审核）：
  - `bom-pending` mine 过滤（混合已审/未审→仅回未审）
  - `bom-pending` all 分页过滤
  - `bom-audit` 复查翻已审=audited true
  - `bom-audit` 复查未翻=audited false 带原文
  - 两端点权限校验（无 `tplus.bom.audit` 403）
- 前端静态（tests/test_bom_builder_page.py）：Tab 锚点、范围切换、审核按钮+确认文案、行内子件展开锚点、刷新、`bom-pending`/`bom-audit` 接线字符串。
- Playwright 冒烟：双 Tab 渲染、切换、空列表提示、无 JS 运行时错误（file:// 静态，不依赖后端）。
- 端到端真机：提交一单→审核 Tab 见到→展开看子件→审核→复查后消失。

## 范围外（YAGNI）

- 批量审核、反审核（UnAudit）、审核历史台账。
- `bom/Audit` 参数格式的静态假设——留实现时现场探定（限流恢复后用测试 BOM）。
