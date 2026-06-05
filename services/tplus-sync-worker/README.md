# tplus-sync-worker

AliECS 内的 T+ 只读同步 worker，初版由 `tplus-datahub` 迁入。当前不接入生产 compose，不写 T+ 数据，只保留 BOM 查询、原始 JSON 保存、Excel 导出和单元测试闭环。

## AliECS 集成边界

- 本服务只负责主动同步 T+ OpenAPI 数据。
- 公网 webhook、验签解密、APP_TICKET/openToken 换取和事件存储由 `services/backend-api` 后续承接。
- `.env` 只允许存在于本地或运行环境，不提交仓库；仓库只保留 `.env.example` 占位值。
- 默认输出目录仍是 `data/raw/`、`output/excel/`、`output/logs/`，这些运行时文件由 `.gitignore` 排除。

## Docker 初版

```bash
docker build -t aliecs-tplus-sync-worker .
```

容器默认执行：

```bash
python -m tplus_datahub.jobs.job_sync_bom
```

生产接入前，需要先明确 openToken 获取流程和运行时密钥注入方式，不建议长期手工写死 `CHANJET_OPEN_TOKEN`。

# 原项目说明

只读对接畅捷通 T+ OpenAPI 的数据同步项目。当前阶段先跑通 BOM 查询、原始 JSON 保存、Excel 导出和日志输出，不做任何写入、创建、修改、删除 T+ 数据的操作。

## 当前功能

- 读取 `.env` 环境变量并校验必要配置。
- 统一封装 T+ OpenAPI POST 客户端。
- 实现 BOM 分页查询闭环。
- 保存每页原始响应到 `data/raw/bom/`。
- 导出清洗后的 Excel 到 `output/excel/`。
- 为物料、成品、采购价格、销售价格、成本等模块预留接口结构。

## 安装

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 配置

复制 `.env.example` 为 `.env`，填写真实配置：

```env
CHANJET_APP_KEY=你的 AppKey
CHANJET_APP_SECRET=你的 AppSecret
CHANJET_OPEN_TOKEN=你的 openToken
```

不要提交 `.env`，不要把 AppSecret 或 openToken 写入代码、文档或测试。

## 运行 BOM 同步

Windows 命令行：

```bat
scripts\run_sync_bom.bat
```

或手动运行：

```powershell
$env:PYTHONPATH="src"
python -m tplus_datahub.jobs.job_sync_bom
```

## 输出位置

- 原始 JSON：`data/raw/bom/`
- Excel：`output/excel/`
- 日志：`output/logs/`

## 已实现接口

- BOM 分页查询：`/tplus/api/v2/bom/QueryPage`

## 待确认接口

原材料、成品、采购价格、销售价格、成本、库存、供应商、客户、销售单据、采购单据接口路径待从官方文档确认。

## 验证

```powershell
$env:PYTHONPATH="src"
python -m compileall .
python -m unittest discover -s tests -v
```

## Production long-running worker

- Production Compose runs `tplus-sync-worker` with `restart: always`.
- The container command is `python -m tplus_datahub.jobs.worker_loop`.
- The first sync starts immediately; later cycles wait `TPLUS_SYNC_INTERVAL_SECONDS` seconds, default `3600`.
- Current `job_sync_all` runs verified read-only `QueryPage` sync for BOM, inventory, and partner records.
- `inventory` is the T+ 存货 archive. `partner` is the T+ 往来单位 archive and covers customer/supplier style counterparties at this stage.
- Sales, purchase, warehouse, price, and cost modules remain pending until their official read-only endpoints are confirmed against the current account and covered by tests.
- Production output is written inside the container to `/app/data` and `/app/output`, backed by Docker volumes `tplus_sync_data` and `tplus_sync_output`.
- Real `CHANJET_APP_KEY`, `CHANJET_APP_SECRET`, and `CHANJET_OPEN_TOKEN` must stay in ECS/KMS runtime config, never in Git.

## Manual BOM sync requests

- `backend-api` writes request files to `TPLUS_BOM_SYNC_REQUEST_DIR` when the homepage `手动同步配方` button is clicked.
- `worker_loop` polls the same directory every `TPLUS_SYNC_POLL_SECONDS` seconds while waiting between scheduled full sync runs.
- A request triggers only `job_sync_bom`, not inventory, partner, Feishu, WeCom, or other sync jobs.
- BOM sync defaults include both enabled and disabled rows, so manual recipe sync is intended to refresh all formulas including `停用` BOMs.
- Finished request files are renamed with `.done`; failed ones are renamed with `.failed`.
