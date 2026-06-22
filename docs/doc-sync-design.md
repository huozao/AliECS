# 文档同步服务设计

## 目的

AliECS 需要把企业微信智能表格和飞书多维表格同步成 Postgres 中可查询的数据，供后台接口读取。第一版目标是完整同步，不做最近记录截断，不只同步表单入口，也不只同步字段。

## 为什么从 PeiFang 迁移

PeiFang 已经跑通了企业微信智能表格 API 的基础能力，包括 access_token 获取、字段读取、sheet 列表读取、records 分页读取、多公司 profile 环境变量读取，以及企业微信错误摘要。这些能力适合作为 AliECS 的同步客户端基础。

AliECS 不直接使用 PeiFang 的 `output/latest` 或 `data/wecom` 作为主数据源。AliECS 的主数据源是 Postgres，worker 只借鉴同步逻辑和配置思路。

## 为什么不直接替换成 wecom-cli

`WecomTeam/wecom-cli` 可以作为官方能力参考，但当前优先保留 PeiFang 已经验证过的 Python API 同步逻辑。这样可以更清楚地控制分页、字段归一化、record_hash、Postgres upsert 和中文错误日志。后续如果 CLI 能补充能力，再单独封装。

## 服务边界

- `doc-sync-worker`：读取环境变量，调用企业微信或飞书 API，完整同步字段、sheet/table、records，写入 Postgres 和同步运行日志。
- `backend-api`：不直接调用企业微信或飞书 API，只提供同步结果、同步状态、同步日志查询接口，并创建后台手动同步请求。
- `admin-ui`：提供“企微或飞书同步”后台模块，按公司分组展示同步表格，用于人工核验数据。
- `postgres`：保存同步源、字段、记录、运行日志。

## 完整同步流程

### 企业微信

1. 读取 `WECOM_ENV_PROFILES`，例如 `COMPANY_A,COMPANY_B`。
2. 对每个 profile 读取企业微信凭证和智能表格 docid。docid 可以来自环境变量，也可以来自 Postgres 中 `external_sources.source_type = 'smartsheet_doc'` 或 `registry_doc` 的登记行。
3. 对每个 docid 调用企业微信 `get_sheet`，拿到全部 sheet。
4. 对每个 sheet 调用 `get_fields`，保存字段定义。
5. 对每个 sheet 调用 `get_records`，按 `has_more` 和 `next` 分页直到结束。
6. 对每条 record 计算 `record_hash`。
7. 用 `source_id + external_record_id` 判断是否已存在。
8. hash 相同不重复更新，hash 变化时更新 `raw_json`、`normalized_json` 和 `synced_at`。
9. 写入 `sync_runs`，记录成功、失败、sheet 数、record 数、创建数、更新数和错误摘要。

### 飞书

1. 读取 `FEISHU_ENV_PROFILES`，例如 `COMPANY_A,COMPANY_B`。
2. 对每个 profile 读取 `FEISHU_<PROFILE>_APP_ID` 和 `FEISHU_<PROFILE>_APP_SECRET`。
3. 从 `FEISHU_<PROFILE>_APP_TOKEN` 直接读取多维表格 token，或从 `FEISHU_<PROFILE>_WIKI_NODE_TOKEN` 解析出 app token。
4. 对每个 `FEISHU_<PROFILE>_TABLE_ID` 调用飞书 bitable 字段接口，保存字段定义。
5. 调用 records 接口按 `has_more` 和 `page_token` 分页直到结束。
6. 对每条 record 计算 `record_hash`。
7. 用 `source_id + external_record_id` 判断是否已存在。
8. hash 相同不重复更新，hash 变化时更新 `raw_json`、`normalized_json` 和 `synced_at`。
9. 写入 `sync_runs`，记录成功、失败、table 数、record 数、创建数、更新数和错误摘要。

## 表结构

- `external_sources`：一个外部数据源。当前按企业微信 docid + sheet_id 落库，每个 sheet 一条 source。
- `external_sources` 中 `source_type = smartsheet_doc` 或 `registry_doc` 的行可以作为登记表，worker 会读取这些行里的 `external_doc_id` 作为待同步 docid。
- `external_fields`：字段元数据，保存企业微信字段 id、字段标题、字段类型和原始 JSON。
- `external_records`：完整记录数据，保存原始 JSON、字段标题展平后的 JSON、hash 和同步时间。
- `sync_runs`：一次同步运行日志，保存 profile、模式、状态、数量统计和错误 JSON。
- `sync_requests`：后台人工发起的手动同步请求。`backend-api` 只写请求，`doc-sync-worker` 负责消费请求并调用外部 API。

## 环境变量

通用：

```bash
WECOM_ENV_PROFILES=COMPANY_A,COMPANY_B
```

每个公司 profile 支持：

```bash
WECOM_COMPANY_A_CORP_ID=
WECOM_COMPANY_A_APP_SECRET=
WECOM_COMPANY_A_APP_SECRET_2=
WECOM_COMPANY_A_APP_SECRETS=
WEDOC_COMPANY_A_DOCID=
WEDOC_COMPANY_A_SHEET_ID=
WEDRIVE_COMPANY_A_SPACEIDS=
SMARTSHEET_COMPANY_A_ID=
SMARTSHEET_COMPANY_A_SHEET_ID=
```

`COMPANY_B` 同理。示例文件中只能放空值或本地测试值，真实凭证只放本机 `local/.env.local` 或 ECS 私有环境文件，不提交 GitHub。

飞书每个公司 profile 支持：

```bash
FEISHU_ENV_PROFILES=COMPANY_A,COMPANY_B
FEISHU_COMPANY_A_APP_ID=
FEISHU_COMPANY_A_APP_SECRET=
FEISHU_COMPANY_A_APP_TOKEN=
FEISHU_COMPANY_A_TABLE_ID=
FEISHU_COMPANY_A_VIEW_ID=
FEISHU_COMPANY_A_WIKI_NODE_TOKEN=
FEISHU_COMPANY_A_WIKI_URL=
FEISHU_COMPANY_A_APP_NAME=
FEISHU_COMPANY_A_TABLE_NAME=
```

`APP_TOKEN` 和 `WIKI_NODE_TOKEN` 二选一，`TABLE_ID` 必填。真实飞书 app secret、app token、tenant token 不得提交 GitHub。

## 本地运行

先准备本地环境：

```bash
copy local\.env.local.example local\.env.local
```

按需填写本地测试用企业微信变量后运行：

```bash
docker compose -f local/docker-compose.local.yml run --rm doc-sync-worker python -m app.main sync-wecom-full
```

也可以在 worker 容器内运行：

```bash
python -m app.main sync-wecom-full --profiles COMPANY_A,COMPANY_B
python -m app.main sync-feishu-full --profiles COMPANY_A,COMPANY_B
```

本地查询接口：

- `GET /v1/admin/doc-sync/sources`
- `GET /v1/admin/doc-sync/runs`
- `GET /v1/admin/doc-sync/records`
- `GET /v1/admin/doc-sync/sources/{source_id}/records`
- `GET /v1/admin/doc-sync/requests`
- `POST /v1/admin/doc-sync/sources/{source_id}/sync-requests`

这些接口需要管理员登录 token。

Admin UI 后台入口：

- 打开 `http://localhost:8081`
- 使用管理员账号登录。
- 查看“企微或飞书同步”模块。
- 页面会按 `env_profile` 分组展示 `external_sources` 中的同步表格。
- “打开原表格”会新标签打开企业微信原始链接。
- “手动同步”只创建请求，不在 backend-api 内直接调用企业微信 API。

消费手动同步请求：

```bash
docker compose -f local/docker-compose.local.yml run --rm doc-sync-worker python -m app.main consume-sync-requests --limit 10
```

按指定 source 同步：

```bash
docker compose -f local/docker-compose.local.yml run --rm doc-sync-worker python -m app.main sync-wecom-source --source-id 1
```

## ECS 运行

建议在 ECS 上用 cron 或 systemd timer 调用 worker。命令示例：

```bash
docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main sync-wecom-full
docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main sync-feishu-full --profiles COMPANY_A,COMPANY_B
```

不要在 `backend-api` 启动流程里自动同步，避免 API 启动被外部企业微信接口阻塞。

消费后台手动请求：

```bash
docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main consume-sync-requests --limit 10
```

## 企微/飞书文档结构备份

备份文档固定包含 `企微A-最新结构`、`企微B-最新结构`、`飞书-最新结构`、`结构变更历史`。每行代表一个企微智能表格文档或飞书多维表格应用，只保存文档定位 ID、工作表编码/名称及规范化字段结构，不读取或写入源业务记录内容。企微最新表按 `企业配置:docid` 更新，飞书按 `FEISHU:企业配置:app_token` 更新；结构哈希变化时才追加一行历史。

总体字段固定排在工作表明细前面：`平台`、`来源类型`、`智能表格名称`、`工作表数量`、`字段总数`、`来源链接`、`企业配置`、`状态`、`文档定位ID`、`docid`。企微 API 不支持原地移动字段；检测到旧顺序时，worker 会先创建临时工作表、复制并核对记录，再替换旧表。

备份文档自身登记为企微 A 的 `structure_backup_doc`，只采集四张备份工作表的字段结构，不采集其中记录。飞书按 app_token 建立 `bitable_app` 锚点，其下每个 table_id 作为一个工作表槽位写入 `飞书-最新结构`。

首次在企微 A 创建备份文档：

```bash
docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main bootstrap-wecom-structure-backup
```

把输出的 `docid` 写入 ECS 私有 `release-meta.env`，再启用：

```bash
WECOM_STRUCTURE_BACKUP_ENABLED=true
WECOM_STRUCTURE_BACKUP_DOCID=dc_xxx
WECOM_STRUCTURE_BACKUP_PROFILE=COMPANY_A
WECOM_STRUCTURE_BACKUP_MAX_SHEETS=20
```

手工生成并消费全部结构备份任务：

```bash
docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main sync-wecom-structure-backup
```

常驻 worker 每日完成企微和飞书全量同步后自动执行；网页“创建副本”完成首次同步后也会自动入队。失败任务保存在 `wecom_structure_backup_jobs`，按指数退避重试。

## 常见错误

- 缺少 `WECOM_CORP_ID` / `WECOM_APP_SECRET`：检查 profile 变量名，例如 `WECOM_COMPANY_A_CORP_ID`。
- 企业可信 IP 未配置：企业微信可能拒绝调用，需要把 ECS 出口 IP 加到可信 IP。
- 企业微信 `errcode 60020`：通常是可信 IP 或应用权限问题，查看日志里的 `from_ip` 摘要。
- `invalid docid`：检查 `WEDOC_COMPANY_A_DOCID` 或 `SMARTSHEET_COMPANY_A_ID` 是否是智能表格 docid。
- `no access`：检查企业微信应用是否有该文档权限，或更换对应公司 profile 的应用 secret。
- 分页中断：worker 会记录失败 run；排查网络、代理、接口限流和 `next` 游标返回。
- 飞书 `has_more=true` 但缺少或重复 `page_token`：worker 会中断本次 run，避免静默漏数据或死循环。
- 飞书鉴权失败：检查应用是否已开通多维表格权限、表格是否授权给应用、ECS 出口 IP 是否满足租户限制。
