# 环境变量矩阵（开发 / 部署 / 验证）

> 目标：让人和 AI 在修改配置时可快速判断“变量影响范围、缺失后果、验证方式”。

## 1. 线上验证（E2E）变量

| 变量名 | 作用 | 是否必填 | 缺失后果 |
|---|---|---|---|
| `APP_BASE_URL` | 项目部署根地址 | 建议 | 验证脚本可能使用错误域名 |
| `E2E_BASE_URL` | 端到端验证根地址（建议用于 API 基准） | 建议 | 端到端脚本无法统一入口 |
| `E2E_PUBLIC_URL` | 公网首页 | 建议 | 无法验证 public-web 入口 |
| `E2E_ADMIN_URL` | 管理后台入口 | 建议 | 无法验证 admin-ui 入口 |
| `E2E_COUPLE_URL` | Couple 页面入口 | 建议 | 无法验证 couple 入口 |
| `E2E_LOGIN_USERNAME`/`E2E_LOGIN_EMAIL` | 测试账号标识 | 建议（二选一） | 无法执行登录 smoke |
| `E2E_LOGIN_PASSWORD` | 测试账号密码 | 建议 | 无法执行登录 smoke |
| `E2E_LOGIN_ROLE` | 预期角色校验 | 可选 | 无法做角色一致性断言 |

> 注意：生产路由有 `/api` 前缀时，建议 `E2E_BASE_URL` 设置为 `https://<domain>/api`。

## 2. 鉴权与管理员引导

| 变量名 | 作用 | 是否必填 | 缺失后果 |
|---|---|---|---|
| `AUTH_TOKEN_SECRET` | JWT 签名密钥 | 生产必填 | 鉴权安全性不足或无法按预期工作 |
| `AUTH_TOKEN_TTL_SECONDS` | token 过期时间 | 建议 | 使用默认值，可能与预期不一致 |
| `ADMIN_BOOTSTRAP_USERNAME` | 初始化管理员用户名 | 建议 | 使用默认值，增加误用风险 |
| `ADMIN_BOOTSTRAP_PASSWORD` | 初始化管理员密码 | 生产必填 | 默认密码风险极高 |
| `ADMIN_BOOTSTRAP_DISPLAY_NAME` | 初始化管理员展示名 | 可选 | 使用默认值 |
| `CORS_ALLOWED_ORIGINS` | 额外允许跨域来源（逗号分隔） | 建议 | HTTPS 域名未加入时，浏览器可能拦截接口请求 |

## 3. Couple 功能入口控制

| 变量名 | 作用 | 是否必填 | 缺失后果 |
|---|---|---|---|
| `COUPLE_FEATURE_ENABLED` | Couple 入口总开关 | 建议 | 入口判定不符合预期 |
| `COUPLE_ROUTE` | Couple 路径 | 建议 | 前后端入口不一致 |
| `COUPLE_ALLOWED_USERS` | 用户白名单 | 建议 | 授权边界不明确 |
| `COUPLE_ALLOWED_EMAILS` | 邮箱白名单 | 可选 | 无法按邮箱维度限制 |
| `MAX_UPLOAD_MB` | Couple 图片上传大小上限 | 建议，默认 15 | 上传限制不符合预期 |
| `STORAGE_DRIVER` | 照片存储驱动：`local` / `webdock` / `oss` | 建议，生产用 webdock | 生产照片存储策略不明确 |
| `LOCAL_UPLOAD_DIR` | local 驱动上传目录，容器内默认 `/app/uploads` 持久卷 | local 必填 | local 上传无法落盘或落到易失目录 |
| `WEBDOCK_PHOTO_BASE_URL` | 旧电脑 WebDock 照片存储 API 根地址 | `STORAGE_DRIVER=webdock` 时必填，默认 `http://host.docker.internal:11800` | backend-api 无法把照片写入旧电脑 |
| `WEBDOCK_PHOTO_API_TOKEN` | 调用旧电脑 WebDock 照片存储 API 的 bearer token | `STORAGE_DRIVER=webdock` 时必填 | 上传/读取照片会失败 |
| `WEBDOCK_PHOTO_TIMEOUT_SECONDS` | 旧电脑照片 API 超时 | 可选，默认 30 | 旧电脑慢响应时行为不明确 |
| `IMMICH_ENABLED` | Couple -> Immich API 集成开关 | 可选，默认 `false` | 关闭时 Couple 只使用现有 local/WebDock 照片通道 |
| `IMMICH_BASE_URL` | Immich API 根地址，不带尾随 `/` | `IMMICH_ENABLED=true` 时必填，建议 `https://immich.hydwang.xyz` | 无法查询或绑定 Immich 照片 |
| `IMMICH_API_KEY` | Immich API key | `IMMICH_ENABLED=true` 时必填；真实值只能在运行时 `.env` 或 secret store | Immich API 调用失败；严禁提交真实值 |
| `IMMICH_TIMEOUT_SECONDS` | Immich API 超时 | 可选，默认 20 | Immich 慢响应时行为不明确 |
| `IMMICH_PROXY_MODE` | Immich 缩略图/内容代理模式 | 可选，默认 `backend` | 浏览器可能暴露不该暴露的 Immich 访问细节 |
| `OSS_ENDPOINT` / `OSS_BUCKET` | OSS 目标地址与 bucket | `STORAGE_DRIVER=oss` 时必填 | OSS 上传不可用 |
| `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` | OSS 凭据 | `STORAGE_DRIVER=oss` 时必填 | OSS 上传不可用 |
| `OSS_TIMEOUT_SECONDS` | OSS 请求超时（秒） | 可选，默认 30 | 网络异常时请求可能挂起更久 |
| `SHARE_BASE_URL` | 分享链接生成根地址 | 建议 | 分享 URL 可能使用错误域名 |

> 当前 backend-api 已抽象 `local` / `webdock` / `oss` driver；`webdock` 通过 ECS 到旧电脑的 WebDock 隧道保存原图，`oss` 通过 Aliyun OSS V1 签名客户端保存原图。

## 4. 维护要求

1. 新增变量时必须同步更新本文档与示例配置文件。
2. 不得提交真实生产密钥、密码、token、私钥。
3. 变量缺失时，代码应给出明确报错或降级行为，避免静默失败。

## 5. WeChat Customer Service API

| Variable | Scope | Purpose | Required |
|---|---|---|---|
| `WECOM_KF_CORP_ID` | backend-api | CorpID used for callback receive-id verification and gettoken | Optional; deploy defaults to `WECOM_COMPANY_B_CORP_ID` |
| `WECOM_KF_APP_SECRET` | backend-api | Dedicated Secret shown under WeChat Customer Service internal access | Required; no self-built-app fallback |
| `WECOM_KF_CALLBACK_TOKEN` | backend-api | Static callback signature token configured in the management console | Optional; deploy defaults to `WECOM_COMPANY_B_CALLBACK_TOKEN` |
| `WECOM_KF_CALLBACK_AES_KEY` | backend-api | 43-character callback EncodingAESKey | Optional; deploy defaults to `WECOM_COMPANY_B_CALLBACK_AESKEY` |
| `WECOM_KF_API_TIMEOUT_SECONDS` | backend-api | Timeout for WeCom API calls | Optional, default 10 |
| `WECOM_KF_POLL_INTERVAL_SECONDS` | backend-api | Fallback poll interval for kf sync_msg when callbacks are not delivered; `0` disables | Optional, default 300 |
| `WECOM_KF_PROCESSOR_URL` | backend-api | OpenAI-compatible local processor; empty means echo | Optional; production deploy defaults to local bridge |
| `WECOM_KF_PROCESSOR_TIMEOUT_SECONDS` | backend-api | Timeout for the optional processor | Optional, default 1260 |
| `WECOM_KF_TASK_STORAGE_DIR` | backend-api | Persistent root for original KF attachments and confirmed task manifests | Optional, default `/app/wecom-kf-materials` |
| `WECOM_KF_TASK_MAX_ATTACHMENTS` | backend-api | Maximum attachments forwarded to the processor per material task | Optional, default 12 |
| `WECOM_KF_TASK_MAX_TOTAL_MB` | backend-api | Maximum combined attachment bytes forwarded to the processor per material task | Optional, default 40 |

The callback's dynamic XML `Token` is a short-lived `sync_msg` token and is not
the same value as `WECOM_KF_CALLBACK_TOKEN`. Never persist either token in logs.

## 6. Chanjet webhook gateway

| Variable | Scope | Purpose | Required |
|---|---|---|---|
| `CHANJET_APP_KEY` | backend-api / tplus-sync-worker | Chanjet app identifier | Required for token exchange |
| `CHANJET_APP_SECRET` | backend-api / tplus-sync-worker | Chanjet app secret | Required for token exchange |
| `CHANJET_WEBHOOK_AES_KEY` | backend-api | 16-byte Chanjet message decrypt key | Required for encrypted webhook decoding |
| `CHANJET_EVENT_SPOOL_DIR` | backend-api | Runtime-only decoded event spool directory | Recommended for first integration pass |
| `CHANJET_AUTO_EXCHANGE_OAUTH_CODE` | backend-api | Whether OAuth callback exchanges `code` immediately | Optional, default false |

Do not commit real AppKey, AppSecret, AES key, appTicket, certificate, auth code, refresh token, or openToken.

## 7. T+ sync worker

| Variable | Scope | Purpose | Required |
|---|---|---|---|
| `CHANJET_BASE_URL` | tplus-sync-worker | Chanjet OpenAPI base URL | Optional, default `https://openapi.chanjet.com` |
| `CHANJET_OPEN_TOKEN` | tplus-sync-worker | T+ OpenAPI read-only sync token | Required for the worker |
| `DEFAULT_PAGE_SIZE` | tplus-sync-worker | T+ QueryPage page size | Optional, default 500 |
| `REQUEST_TIMEOUT_CONNECT` / `REQUEST_TIMEOUT_READ` | tplus-sync-worker | T+ OpenAPI request timeouts | Optional |
| `TPLUS_SYNC_INTERVAL_SECONDS` | tplus-sync-worker | Seconds between long-running full reconciliation cycles; first run starts immediately | Optional, default 86400 |
| `TPLUS_SYNC_POLL_SECONDS` | tplus-sync-worker | Poll interval for manual BOM sync request files during long sleeps | Optional, default 30 |
| `TPLUS_DB_SYNC_REQUESTS_ENABLED` | tplus-sync-worker | Whether the worker polls Postgres `integration_sync_requests` for event-driven BOM sync | Optional, default true |
| `TPLUS_BOM_SYNC_REQUEST_DIR` | backend-api / tplus-sync-worker | Shared runtime directory for homepage "manual sync recipe" requests | Optional |

Production stores worker output in Docker volumes mounted at `/app/data` and `/app/output`.
The current long-running worker scope is verified read-only `QueryPage` sync for BOM, inventory, and partner records. Other T+ modules must be added only after confirming their official read-only endpoints.
Homepage manual recipe sync only writes a request file for the BOM worker path. The worker consumes it by running `job_sync_bom`, whose default BOM sync queries both enabled and disabled BOM rows.

## 8. Ops Health

| Variable | Scope | Purpose | Required |
|---|---|---|---|
| `OPS_HEALTH_HTTP_TARGETS_JSON` | backend-api | Optional JSON list of HTTP targets to show on `/health/`, for example old laptop or WebDock endpoints. If unset, backend-api probes AliECS public web/API plus default WebDock API/noVNC targets. | Optional, unset uses built-in defaults |

Default WebDock health targets use the ECS-side SSH tunnel host alias `host.docker.internal` and port `11800`, not the old Tailscale address. Compose maps `host.docker.internal` to the Docker host through `host-gateway`. ECS installs `webdock-tunnel-proxy.service` to forward Docker-host traffic on `172.17.0.1:11800` to the existing reverse SSH tunnel on `127.0.0.1:11800`; this keeps the SSH tunnel private to ECS while making it reachable from containers. noVNC should be added through `OPS_HEALTH_HTTP_TARGETS_JSON` or `OPS_HEALTH_WEBDOCK_NOVNC_URL` only after a reachable tunnel is configured.

| Variable | Scope | Purpose | Required |
|---|---|---|---|
| `WEBDOCK_TUNNEL_PROXY_ENABLED` | ECS deploy host | When `true`, `deploy/ecs/deploy.sh` installs or refreshes `webdock-tunnel-proxy.service`. | Optional, default `true` |
| `WEBDOCK_TUNNEL_PROXY_BIND_HOST` | ECS deploy host | Host address exposed to Docker containers. Default is Docker bridge gateway `172.17.0.1`. | Optional |
| `WEBDOCK_TUNNEL_PROXY_BIND_PORT` | ECS deploy host | Host port exposed to Docker containers. Keep `11800` to match `host.docker.internal:11800`. | Optional |
| `WEBDOCK_TUNNEL_PROXY_TARGET_HOST` | ECS deploy host | Local reverse SSH tunnel host, normally `127.0.0.1`. | Optional |
| `WEBDOCK_TUNNEL_PROXY_TARGET_PORT` | ECS deploy host | Local reverse SSH tunnel port, normally `11800`. | Optional |
| `WEBDOCK_TUNNEL_PROXY_BACKLOG` | ECS deploy host | TCP listen backlog for the local proxy. | Optional |

## 9. Recipe query API

| Variable | Scope | Purpose | Required |
|---|---|---|---|
| `RECIPE_BOM_INPUT_DIR` | backend-api | Directory scanned for the latest T+ BOM workbook | Optional, default `/app/tplus-output/excel` |
| `RECIPE_BOM_INPUT_GLOB` | backend-api | Semicolon-separated workbook filename patterns | Optional |
| `RECIPE_ACTIVE_BOM_DIR` | backend-api | Persistent directory for the human-confirmed active BOM workbook generated after reconciliation | Optional, default `/app/recipe-active-bom` |
| `RECIPE_EXPORT_DIR` | backend-api | Temporary directory for generated recipe query workbooks | Optional, default `/tmp/aliecs-recipe-exports` |

`backend-api` mounts the T+ worker output volume read-only. Confirmed reconciliation files are written to `RECIPE_ACTIVE_BOM_DIR` and are preferred by recipe query before the raw worker output. Query outputs are generated per request and should be treated as temporary files, not source data.

## 10. Feishu full sync worker

| Variable | Scope | Purpose | Required |
|---|---|---|---|
| `FEISHU_ENV_PROFILES` | doc-sync-worker | Comma-separated company profiles, for example `COMPANY_A,COMPANY_B` | Required for `sync-feishu-full` unless profiles are passed by CLI |
| `FEISHU_<PROFILE>_APP_ID` | doc-sync-worker | Feishu app ID for the profile | Required |
| `FEISHU_<PROFILE>_APP_SECRET` | doc-sync-worker | Feishu app secret for the profile | Required |
| `FEISHU_<PROFILE>_APP_TOKEN` | doc-sync-worker | Bitable app token | Required unless `WIKI_NODE_TOKEN` is configured |
| `FEISHU_<PROFILE>_TABLE_ID` | doc-sync-worker | Bitable table ID | Required |
| `FEISHU_<PROFILE>_VIEW_ID` | doc-sync-worker | Optional bitable view filter | Optional |
| `FEISHU_<PROFILE>_WIKI_NODE_TOKEN` | doc-sync-worker | Wiki node token used to resolve app token | Optional alternative to `APP_TOKEN` |
| `FEISHU_<PROFILE>_WIKI_URL` | doc-sync-worker | Human-readable source URL saved with source metadata | Optional |
| `FEISHU_<PROFILE>_APP_NAME` / `FEISHU_<PROFILE>_TABLE_NAME` | doc-sync-worker | Source display name | Optional |

Do not commit real Feishu app IDs, app secrets, app tokens, wiki node tokens, tenant access tokens, or business table data. Feishu sync writes normalized fields, records, and run diagnostics into Postgres through `doc-sync-worker`.
