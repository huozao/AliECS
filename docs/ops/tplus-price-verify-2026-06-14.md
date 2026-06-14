# T+ 采购/销售价格实拉验证（2026-06-14，已打通）

## 结论

采购价格/销售价格改用**官方报表通用查询** `POST /tplus/api/v2/reportQuery/GetReportData`
（[官方文档](https://open.chanjet.com/md/docs/file/apiFile/tcloud/t+bb/t+tybb?id=30780)），
一次分页查询即出整表，字段/中文标题随接口返回，**取代**原先 `FindVoucherList` + 逐单
`GetVoucherDTO` 扇出方案。已对生产租户逐字段对账，与网页版导出的
`采购价格查询.xlsx` / `销售价格查询.xlsx` **行数与数值完全一致**。

| 模块 | ReportName（报表） | 单据号 | 实拉全量 | 用户Excel数据行 |
| --- | --- | --- | --- | --- |
| purchase_price | `PU_PurchaseArrivalDetailRpt`（采购到货明细表＝采购价格查询） | PS- | **529** | 529 |
| sales_price | `SA_SaleDeliveryDetailRpt`（销货单明细表＝销售价格查询） | SA- | **1151** | 1151 |

锚点对账：`PS-2026-05-0015`（钛白粉R-8800 / 34000 / 单价14.34 / 含税16.20 / 税额63366.37）、
`SA-2026-05-0003`（HYD-1836 / 400 / 单价12.65 / 含税14.30）逐字段一致。

## 接口要点（实测）

- 请求体须包成 `{"request": {...}}`；扁平 body 直接 999。
- 必填：`ReportName` / `PageIndex` / `PageSize` / `SearchItems` / `ReportTableColNames`。
  - `ReportTableColNames` 传逗号分隔字段名；**留空/省略则返回该报表全部列**（用于探查列字典）。
  - `SearchItems`：`[{"ColumnName":"VoucherDate","BeginDefault":"始","BeginDefaultText":"始","EndDefault":"止","EndDefaultText":"止"}]`。
- 翻页：首次不传，响应回 `TaskSessionID`+`SolutionID`，后续页回传二者并递增 `PageIndex`；
  `Pages` 按 `PageSize` 计算（PageSize=500 时 529 行→2 页、1151 行→3 页）。
- 返回：`DataSource.Rows[]`（`rowType=="D"` 为明细行，需过滤合计行）；
  `ColumnSource.Rows[]` 给 `FieldName→中文Title`；`Status==0`、`ErrorMessage` 判错。
- 字段：`discountRate` 是小数（`1.0000`＝100%，导出列 `折扣%` 已 ×100）；`origPrice`(报价) 多为空。
- 鉴权 header：`appKey`/`appSecret`/`openToken`（worker 复用 `ChanjetClient`，openToken 走自动刷新 token 文件）。
  - 关键字段映射见 `config/endpoints.py` 的 `VERIFIED_PRICE_REPORTS`。
- `PU_PurchasePriceRpt`（直译“采购价格”）实测报 `未将对象引用…`（需分组项），**勿用**。

## 离线验证

```powershell
python -m pytest tests/test_tplus_price.py tests/test_backend_exports.py -q   # 10 passed
```

## 线上复跑（部署新镜像后）

```powershell
# 容器内（worker 有 env + 自动刷新 openToken）
docker exec ecs-tplus-sync-worker-1 python -m tplus_datahub.jobs.job_sync_purchase_price
docker exec ecs-tplus-sync-worker-1 python -m tplus_datahub.jobs.job_sync_sales_price
```

产出 `output/excel/purchase_price_*.xlsx` / `sales_price_*.xlsx`，由 backend `_latest_tplus_exports`
扫描后出现在 health 页“数据导出”。全量日期窗口默认 `2015-01-01..今天+1`，可用 env
`PRICE_SYNC_BEGIN_DATE` 调整。
