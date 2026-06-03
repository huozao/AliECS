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
