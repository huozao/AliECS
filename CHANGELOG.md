# 版本记录

## v2.1.16：修复 T+ 写入被标量返回体误杀（真机首单实锤）+ 版本号默认当日日期

- 生产实锤（提交 #1/#2 均倒在此）：T+ Create 类接口**成功**时返回裸标量 JSON（如新记录 ID `568`），`ChanjetClient.post` 的 dict/list 守卫把成功当异常抛 `接口返回 JSON 不是对象或列表结构` → 存货已在 T+ 建成但提交卡 needs_review。守卫移除，标量/null 原样返回，由调用方按语义处理（BOM 写入以写后查询验证为权威）。
- `_result_id` 支持标量响应直接作为新记录 ID；写后验证不通过仍走 needs_review，不放松兜底。
- bom-builder 版本号默认值从固定 `V1` 改为当日日期 `YYMMDD`（如 `260713`，`todayVersion()`），草稿恢复保留用户已填值。

## v2.1.15：bom-builder 新建存货完整选项 + 建议编码查重 + T+ 报错透传 + 桌面两栏

- 新端点 `GET /v1/tplus/inventory-code-suggestion?class_code=`：按约定「分类编码前 2 位 + 6 位流水」扫每日存货导出取最大流水 +1（物料清单首个建议 `06000001`），历史杂乱编码不参与；前端选类别后显示建议条，点击填入。
- `GET /v1/tplus/inventories` 新增 `include_disabled` 参数（默认 false 行为不变）；前端编码输入失焦/停顿 400ms 精确查重（含停用存货），重复红字提示并阻断提交；接口失败只提示不阻塞录入。
- 存货属性 6 项（外购 `IsPurchase`/销售 `IsSale`/自制 `IsMadeSelf`/生产耗用 `IsMaterial`/委外 `IsMadeRequest`/虚拟件 `IsPhantom`，生产实测验证字段名）进 T+ 创建 payload：父件默认勾前 5 项（虚拟件不勾），原料弹窗默认外购+生产耗用；属性全不勾双端拦截；旧草稿缺属性键回退旧 kind 写死逻辑（不引入新键）。
- worker 报错透传：`ChanjetAPIError` 新增 `business_message`（解析 T+ 错误返回体原文，如「存货编号：xxx不唯一…」）；有业务错误文本=确定拒绝转 `failed` 并原文透传到提交卡，无文本（网络类）维持 `needs_review`。
- 前端父件新建区重组三分组（基本信息/计量单位/存货属性，`<details>` 可折叠默认展开）；桌面 >900px 两栏布局（父件左/子件右），手机保持单列；提交状态卡展示存货创建/复用事件与错误原文全文。

## v2.1.14：formula 对比核心抽共享模块 compare-core.js（网站/小程序单源）

- 对比算法（行归一化/版本聚合/基准目标/矩阵/状态判定/筛选/导出payload）从 `formula/index.html` 内联抽出为 `formula/compare-core.js`（UMD 纯函数，禁 ?.,??,Intl 以兼容小程序 iOS JSCore），页面改薄壳委托，渲染代码不动；Playwright 双端口 stub 金样前后对比验证行为不变。
- 权威源=本文件；微信小程序 weapp-lab 持字节一致副本（sync 脚本），行为单测 11 例在 weapp-lab node --test。

## v2.1.13：formula 只读 API token 通道（小程序云函数代理）

- `services/backend-api/app/routers/recipes.py` 新增 `formula_login_or_token` 依赖：请求头 `X-API-Key` 与 env `FORMULA_API_TOKEN` 匹配即放行 5 条只读 formula 路由（query/cost/cost-export/compare-export/download），合成只读权限用户；不匹配回落现有 Bearer 登录鉴权；env 缺失=通道关闭。写路由（sync-bom）及其他域不挂载。
- 供微信小程序云函数 formula-proxy 使用（小程序无 ICP 备案不能直连，云函数出网转发）；token 由 SOPS 管理。
- 新增 `tests/test_backend_formula_token.py` 6 用例：匹配放行只读×5路由 / 错 token 401 / env 缺失关闭 / 写路由不放行 / 错 key 回落 Bearer。
- 顺带修复 `app/recipes/compare_export.py` 潜在 bug：`rows` 为空时 legend 行与冻结窗格目标行重合，`ws.freeze_panes` 赋值 `Cell` 对象落入合并区变 `MergedCell` 导致 500；改赋坐标字符串规避（该场景此前无测试覆盖，由本任务新增用例首次触发）。

## v2.1.12：openclaw-bridge 群策略缓存可推送失效 + bitable 写记录异步化

- 新增 `POST /admin/invalidate-feishu-group-policy` 端点（`X-Admin-Secret` 鉴权，未配 secret 时端点关闭）：bitable 群配置（「回复模式」/「是否启用机器人」）修改后由 bitable 自动化触发，按 `chat_id` 立即清缓存，无需等 TTL；空 `chat_id` 清整表（运维兜底）。
- `FEISHU_GROUP_POLICY_CACHE_SECONDS` 默认 15s → 600s，新增 `OPENCLAW_BRIDGE_FEISHU_POLICY_CACHE_SECONDS` env 覆盖；配合 invalidate 端点可放心拉到 3600+，显著减少 bitable HTTP 扫描频次。
- `append_feishu_session_console_records` 改 fire-and-forget 异步：4 处调用点（已回复/失败/仅记录）由 daemon Thread 接管 5–8 次 bitable 写，节省每请求 2–3s 阻塞；失败仍走原 `print` 路径。details 深拷贝防主线程后续 mutate 串入 worker。
- 新增 5 个 pytest（端点鉴权关/拒错 secret/清单 chat_id/清全表/异步发射+deepcopy/TTL env 读取）。

## v2.1.11：Couple Memory 重建为 App 内闭环

- Couple Dashboard 的地图足迹与相册入口从 AdventureLog 外链收回 App 内，`/map/` 恢复 Leaflet + `/v1/map/memories` 打点，并支持年份/标签筛选。
- 新增 `0016_couple_memory_rebuild.sql`：补齐 Couple 重建所需幂等迁移、Immich 选片字段、分享/照片/成员索引与资产绑定唯一索引。
- Immich 集成补齐搜索代理与缩略图代理；详情页在 `IMMICH_ENABLED=false` 时隐藏选片入口，开启后可搜索并绑定资产。
- 生产/本地配置示例统一 `LOCAL_UPLOAD_DIR=/app/uploads`，本地 compose 增加 Couple 照片存储、WebDock 与 Immich 环境变量及 uploads 持久卷。

## v2.1.10：成本核算系统单价自动取最新采购价 + 对比表展示销售价格

- 成本核算「价格信息」的「系统单价」自动取该子件的**最新采购含税单价**（按最新单据日期、含税单价>0），悬浮显示该价格单据的**单据日期**（标“录入日期”）；无采购记录才回退 BOM 自带系统单价。
- 对比表上方指标的「版本」替换为「销售价格」（取该配方成品父件的最新销售含税单价），位置仍在「当前合价」前，标注价格更新日期（单据日期）。
- 新增 `app/recipes/price_lookup.py`：读取共享卷最新 `purchase_price_*.xlsx` / `sales_price_*.xlsx`，按存货编码取最新含税单价+单据日期；`/v1/recipes/cost(/export)` 注入，读取异常优雅降级。
- 数据导出文案：去掉采购/销售价格表的“（若已同步）”，注明来源与系统单价用途。

## v2.1.9：T+ 采购/销售价格改走官方报表通用查询 + 修复基础档案全量同步截断

- 采购/销售价格改用官方 `reportQuery/GetReportData`（报表 `PU_PurchaseArrivalDetailRpt`＝采购价格查询、`SA_SaleDeliveryDetailRpt`＝销售价格查询），一次分页查询出整表，分页经 `TaskSessionID/SolutionID`，取代原先逐单 `GetVoucherDTO` 扇出；生产实拉与网页导出逐字段一致（采购 529 行 / 销售 1151 行），导出落 `purchase_price_*.xlsx` / `sales_price_*.xlsx` 进 health 数据导出。
- 修复 `base_archive` 同步：原为单次调用、不传 `PageIndex/PageSize`，数据超服务端默认上限会静默截断；改为走 `paginate_query` 显式翻页保证全量（current_stock/warehouse/unit/project/brand/district 等）。实测 `/Query`(V3.0) 完全支持翻页。
- 价格全量起始日下限放宽到 `2000-01-01`（可经环境变量 `PRICE_SYNC_BEGIN_DATE` 覆盖）。
- 测试：重写 `tests/test_tplus_price.py`（reportQuery 响应样本），新增 `tests/test_tplus_base_archive.py`（翻页全量红→绿用例）。

## v2.1.8：新增 coding-executor 与 mcp 编程工具（阶段二只读 dry-run）

- 新增 `services/coding-executor`：开发机本地 FastAPI 服务，bearer 鉴权 + 仓库白名单 + 异步任务，仅执行只读 git 操作（git_status/git_log/git_diff/list_files/read_file），含路径越界与选项注入防护。不进 ECS、不进 compose、不进 release 矩阵。
- `mcp-coding-server` 升级至 v0.2.0：新增 `list_coding_targets`/`start_coding_task`/`get_coding_task`，经反向 SSH 隧道调用开发机 executor；executor 离线时优雅降级。`start_coding_task` 非只读以触发 ChatGPT 写确认。
- `compose.prod.yml`/`deploy.sh`/`runtime.env.example` 增加 `EXECUTOR_BASE_URL`/`EXECUTOR_TOKEN` 透传（默认空）；CI 增加 executor 依赖与测试。

## v2.1.7：新增 mcp-coding-server 编程桥接服务（阶段一只读 PoC）

- 新增 `services/mcp-coding-server`：MCP streamable-http 服务，供 ChatGPT 开发者模式连接器接入；阶段一仅含只读工具 `ping`/`server_info` 与 `/healthz`。
- 接入 CI 依赖安装、release 构建矩阵、`compose.prod.yml`（127.0.0.1:8090）、`deploy.sh` 镜像清单与 `runtime.env.example` 占位。
- 公网暴露由 ECS Nginx 秘密路径 location 承担，属运行时配置，不入库。

## v2.1.6：修复首页登录提交流程并完善 Couple 入口体验

- 修复首页登录弹窗“提交”无效问题。
- 优化 Admin UI 与 Couple 入口展示与跳转逻辑。
- 收敛 PR 辅助工作流触发频率，减少重复检查噪音。


## v2.1.3：当前稳定版本基线

- 记录当前仓库已有的部署、权限、Admin UI、ECS 发布流程状态。

## v2.1.4：完善版本号自动发布流程

- 新增 VERSION 版本号文件。
- 新增 CHANGELOG 中文版本记录。
- GitHub Actions 自动读取 VERSION 作为镜像标签。
- 规范后续提交、PR 标题与版本说明使用中文。
