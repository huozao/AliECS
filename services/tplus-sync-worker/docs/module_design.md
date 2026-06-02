# 模块设计

## 分层

- `config/`：环境变量和接口路径。
- `chanjet/`：OpenAPI 客户端、鉴权参数注入、分页、响应解析。
- `modules/`：各业务模块的同步、清洗、导出入口。
- `storage/`：JSON、Excel、后续 SQLite 等本地写入。
- `jobs/`：命令行任务入口。
- `reports/`：后续分析报表逻辑。

## 当前闭环

`job_sync_bom` 读取配置，调用 `sync_bom` 分页获取数据，保存每页原始 JSON，再通过 `export_bom` 导出 Excel。

## 未确认模块

未确认接口的模块先抛出清晰异常，不调用未知路径。
