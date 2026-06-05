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

## 4. 维护要求

1. 新增变量时必须同步更新本文档与示例配置文件。
2. 不得提交真实生产密钥、密码、token、私钥。
3. 变量缺失时，代码应给出明确报错或降级行为，避免静默失败。

## 5. Chanjet webhook gateway

| Variable | Scope | Purpose | Required |
|---|---|---|---|
| `CHANJET_APP_KEY` | backend-api / tplus-sync-worker | Chanjet app identifier | Required for token exchange |
| `CHANJET_APP_SECRET` | backend-api / tplus-sync-worker | Chanjet app secret | Required for token exchange |
| `CHANJET_WEBHOOK_AES_KEY` | backend-api | 16-byte Chanjet message decrypt key | Required for encrypted webhook decoding |
| `CHANJET_EVENT_SPOOL_DIR` | backend-api | Runtime-only decoded event spool directory | Recommended for first integration pass |
| `CHANJET_AUTO_EXCHANGE_OAUTH_CODE` | backend-api | Whether OAuth callback exchanges `code` immediately | Optional, default false |

Do not commit real AppKey, AppSecret, AES key, appTicket, certificate, auth code, refresh token, or openToken.

## 6. T+ sync worker

| Variable | Scope | Purpose | Required |
|---|---|---|---|
| `CHANJET_BASE_URL` | tplus-sync-worker | Chanjet OpenAPI base URL | Optional, default `https://openapi.chanjet.com` |
| `CHANJET_OPEN_TOKEN` | tplus-sync-worker | T+ OpenAPI read-only sync token | Required for the worker |
| `DEFAULT_PAGE_SIZE` | tplus-sync-worker | T+ QueryPage page size | Optional, default 500 |
| `REQUEST_TIMEOUT_CONNECT` / `REQUEST_TIMEOUT_READ` | tplus-sync-worker | T+ OpenAPI request timeouts | Optional |
| `TPLUS_SYNC_INTERVAL_SECONDS` | tplus-sync-worker | Seconds between long-running sync cycles; first run starts immediately | Optional, default 3600 |
| `TPLUS_SYNC_POLL_SECONDS` | tplus-sync-worker | Poll interval for manual BOM sync request files during long sleeps | Optional, default 30 |
| `TPLUS_BOM_SYNC_REQUEST_DIR` | backend-api / tplus-sync-worker | Shared runtime directory for homepage "manual sync recipe" requests | Optional |

Production stores worker output in Docker volumes mounted at `/app/data` and `/app/output`.
The current long-running worker scope is verified read-only `QueryPage` sync for BOM, inventory, and partner records. Other T+ modules must be added only after confirming their official read-only endpoints.
Homepage manual recipe sync only writes a request file for the BOM worker path. The worker consumes it by running `job_sync_bom`, whose default BOM sync queries both enabled and disabled BOM rows.

## 7. Recipe query API

| Variable | Scope | Purpose | Required |
|---|---|---|---|
| `RECIPE_BOM_INPUT_DIR` | backend-api | Directory scanned for the latest T+ BOM workbook | Optional, default `/app/tplus-output/excel` |
| `RECIPE_BOM_INPUT_GLOB` | backend-api | Semicolon-separated workbook filename patterns | Optional |
| `RECIPE_EXPORT_DIR` | backend-api | Temporary directory for generated recipe query workbooks | Optional, default `/tmp/aliecs-recipe-exports` |

`backend-api` mounts the T+ worker output volume read-only. Query outputs are generated per request and should be treated as temporary files, not source data.

## 8. Feishu full sync worker

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
