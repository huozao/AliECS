# T+ 采购/销售价格实时验证记录（2026-06-14）

## 离线实现

- 已实现 `purchase_price` / `sales_price`：`FindVoucherList` 分页取单据 ID，再逐单调用 `GetVoucherDTO`，从 `data.Details[]` 拍平成价格行。
- 已按现有 T+ worker 范式复用 `Settings`、`ChanjetClient`、raw JSON 保存和 `export_rows_to_excel`。
- 当前未新增数据库迁移：现有 voucher/base archive 模块也只保存 raw JSON 和 Excel。
- `GetVoucherDTO` 请求体暂按 `{"id": "<voucher_id>"}` 实现；需对照当前畅捷通 T+ 版本文档和实拉结果复核。
- 采购先接 `PurchaseArrivalOpenApi`，销售先接 `SaleDeliveryOpenApi`；需实拉确认是否与用户导出的价格查询表一致。

## 已跑离线验证

```powershell
$env:PYTHONPATH='services/tplus-sync-worker/src'; pytest tests/test_tplus_price.py -v
```

结果：`5 passed`。

```powershell
$env:PYTHONPATH='.'; pytest tests/test_backend_exports.py::BackendExportsTests::test_latest_tplus_exports_includes_short_descriptions -v
```

结果：`1 passed`。

## 实时验证结果

仓库根目录仅设置 `PYTHONPATH=services/tplus-sync-worker/src` 时，现有 T+ 子项目无法导入 `config`；本次实时验证在 `services/tplus-sync-worker` 目录下用 `PYTHONPATH=src` 运行。

```powershell
$env:PYTHONPATH='src'; python -m tplus_datahub.jobs.job_sync_purchase_price
$env:PYTHONPATH='src'; python -m tplus_datahub.jobs.job_sync_sales_price
```

结果：两个 job 均按 3 次重试后退出，未产出价格 xlsx。

- `purchase_price`：`/tplus/api/v2/PurchaseArrivalOpenApi/FindVoucherList` 返回 HTTP 403，`message=openToken已失效`。
- `sales_price`：`/tplus/api/v2/SaleDeliveryOpenApi/FindVoucherList` 返回 HTTP 403，`message=openToken已失效`。

## ⚠️ 人工/ops 步骤

1. 刷新或恢复有效的 `CHANJET_OPEN_TOKEN` / `CHANJET_OPEN_TOKEN_FILE`。
2. 在 `services/tplus-sync-worker` 目录运行：

```powershell
$env:PYTHONPATH='src'; python -m tplus_datahub.jobs.job_sync_purchase_price
$env:PYTHONPATH='src'; python -m tplus_datahub.jobs.job_sync_sales_price
```

3. 对比生成的 `output/excel/purchase_price_*.xlsx` 与用户导出的 `采购价格查询.xlsx`：行数量级、`含税单价`、`数量`、`存货编码` 抽样一致。
4. 对比生成的 `output/excel/sales_price_*.xlsx` 与用户导出的 `销售价格查询.xlsx`：行数量级、`含税单价`、`数量`、`存货编码` 抽样一致。
5. 若字段名不一致，按当前 T+ 版本文档修正 `transform_purchase_price.py` / `transform_sales_price.py` 的映射。
