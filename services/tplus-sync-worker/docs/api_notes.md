# API 记录

## 已确认接口

| 模块 | 方法 | 路径 | 状态 |
| --- | --- | --- | --- |
| BOM | POST | `/tplus/api/v2/bom/QueryPage` | 已按需求配置 |

## 待确认接口

- material：待确认接口
- product：待确认接口
- purchase_price：待确认接口
- sales_price：待确认接口
- cost：待确认接口
- inventory：待确认接口
- supplier：待确认接口
- customer：待确认接口
- sales：待确认接口
- purchase：待确认接口

## 授权注意事项

当前客户端集中在 `src/tplus_datahub/chanjet/auth.py` 注入 `appKey`、`appSecret`、`openToken`。授权参数的最终位置和字段名仍应以畅捷通官方文档为准，确认后只需要调整这一处。

## 返回字段观察

暂无真实返回样本。当前解析器兼容 `Result`、`Data`、`Value`、`result`、`data` 以及常见列表字段 `Rows`、`items`、`records` 等。
## 2026-06-02 verified read-only endpoints

These endpoints were confirmed against the current Chanjet/T+ account with `PageSize=1` probes and then full sync:

| Module | T+ entity | Endpoint | Payload shape | Full sync result |
|---|---|---|---|---|
| `bom` | 物料清单 | `/tplus/api/v2/bom/QueryPage` | `{"param":{"PageIndex":1,"PageSize":500}}` | 187 rows |
| `inventory` | 存货 | `/tplus/api/v2/inventory/QueryPage` | `{"param":{"PageIndex":1,"PageSize":500}}` | 501 rows |
| `partner` | 往来单位 | `/tplus/api/v2/partner/QueryPage` | `{"param":{"PageIndex":1,"PageSize":500}}` | 188 rows |

The following read-only-looking endpoints were probed with empty/page-size-one payloads and returned HTTP/status `999` in the current account, so they are not connected to the production loop yet:

- `/tplus/api/v2/warehouse/Query`
- `/tplus/api/v2/SaleOrderOpenApi/FindVoucherList`
- `/tplus/api/v2/PurchaseOrderOpenApi/FindVoucherList`
- `/tplus/api/v2/SaleDeliveryOpenApi/FindVoucherList`
- `/tplus/api/v2/PurchaseArrivalOpenApi/FindVoucherList`
- `/tplus/api/v2/PurchaseReceiveOpenApi/FindVoucherList`
- `/tplus/api/v2/SaleDispatchOpenApi/FindVoucherList`
- `/tplus/api/v2/MaterialDispatchOpenApi/FindVoucherList`

Do not add these modules to `job_sync_all` until their required filter payloads and account permissions are confirmed.

## 2026-06-03 BOM disabled coverage

In the current Chanjet/T+ account, default BOM paging returns enabled records only.
Use two read-only `QueryPage` requests for full BOM coverage:

- enabled BOM: `{"param":{"PageIndex":1,"PageSize":500,"Disabled":"0"}}`
- disabled BOM: `{"param":{"PageIndex":1,"PageSize":500,"Disabled":"1"}}`

Observed counts on 2026-06-03:

- enabled BOM: 187 rows
- disabled BOM: 31 rows

The worker merges both result sets by `ID`/`Code`/`Version`/`Disabled` before exporting BOM.

## 2026-06-04 master data disabled coverage

Live read-only probes against the current Chanjet/T+ account confirmed these `QueryPage`
status splits:

| Module | Endpoint | Default | `Disabled="0"` | `Disabled="1"` | Worker behavior |
|---|---|---:|---:|---:|---|
| `bom` | `/tplus/api/v2/bom/QueryPage` | not used for full sync | 190 | 31 | query both states and merge |
| `inventory` | `/tplus/api/v2/inventory/QueryPage` | 505 | 502 | 3 | query both states and merge |
| `partner` | `/tplus/api/v2/partner/QueryPage` | 188 | 188 | 0 | query both states and merge |

For inventory and partner the API returned top-level `Disabled=None` in rows from both split
queries. The worker keeps raw JSON unchanged, then fills the exported/synced row `Disabled`
field from the query split only when the source value is missing. This makes disabled inventory
records auditable in Excel without changing stored raw responses.

## 2026-06-04 base archive Query coverage

Official AI-friendly markdown docs expose base archive `Query` endpoints under
`https://open.chanjet.com/md/docs/file/apiFile/tcloud`. These endpoints use
`{"param":{}}`, not `PageIndex`/`PageSize`.

Connected to the long-running worker after live read-only probes:

| Module | Endpoint | Observed rows |
|---|---|---:|
| `warehouse` | `/tplus/api/v2/warehouse/Query` | 7 |
| `unit_group` | `/tplus/api/v2/UnitGroup/Query` | 0 |
| `unit` | `/tplus/api/v2/Unit/Query` | 19 |
| `project` | `/tplus/api/v2/Project/Query2` | 0 |
| `project_class` | `/tplus/api/v2/ProjectClass/Query` | 1 |
| `brand` | `/tplus/api/v2/brand/Query` | 0 |
| `district` | `/tplus/api/v2/district/Query` | 0 |

Probed but not connected because the current account returned HTTP/status `999` for
`{"param":{}}`: department, person, marketing organ, settle style, bank account, currency,
expense, income.

## 2026-06-04 voucher list coverage

Official voucher list endpoints use lowercase `pageSize` and `pageIndex` starting from `0`.
They return `data.Columns` plus `data.Rows`; the worker maps each row array to a flat dict
before exporting.

Connected list-level sync after live read-only `pageSize=1` probes:

| Module | Endpoint | Observed total |
|---|---|---:|
| `sale_order_list` | `/tplus/api/v2/SaleOrderOpenApi/FindVoucherList` | 516 |
| `sale_delivery_list` | `/tplus/api/v2/SaleDeliveryOpenApi/FindVoucherList` | 162 |
| `purchase_order_list` | `/tplus/api/v2/PurchaseOrderOpenApi/FindVoucherList` | 362 |
| `purchase_arrival_list` | `/tplus/api/v2/PurchaseArrivalOpenApi/FindVoucherList` | 332 |
| `purchase_receive_list` | `/tplus/api/v2/PurchaseReceiveOpenApi/FindVoucherList` | 446 |
| `material_dispatch_list` | `/tplus/api/v2/MaterialDispatchOpenApi/FindVoucherList` | 625 |

Current voucher sync is list-level with ID/date/code fields only. Full voucher DTO/detail sync
must be added after confirming safe read-only `GetVoucherDTO` fan-out behavior and acceptable
API volume.
