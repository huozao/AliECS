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
