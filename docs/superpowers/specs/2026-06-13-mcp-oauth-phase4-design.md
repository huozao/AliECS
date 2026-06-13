# MCP Coding Route Phase 4：OAuth 2.1 自托管授权 设计

- 日期：2026-06-13
- 状态：设计待评审
- 关联：`mcp-coding-route`（阶段一/二/3a），`services/mcp-coding-server`
- 前置：Phase 3a（worktree 隔离写入）已合并（PR#103）。本阶段是把写工具接入 ChatGPT 连接器之前的**硬前置**。

## 1. 目标与背景

给 `mcp-coding-server` 加 OAuth 2.1，使 ChatGPT 自定义连接器在调用任何 MCP 工具前必须持有有效 access token。

当前状态（连接器面板实测确认）：公网入口 = Nginx 秘密路径，**应用层零鉴权**（「支持/使用的授权方式 = 无」）。Phase 3a 让该入口具备了「写文件 + commit」能力（虽限制在隔离 worktree、不 push/merge），因此在把写工具暴露给连接器之前必须先补鉴权。ChatGPT 连接器只接受 OAuth（不支持 API key / M2M），所以 OAuth 是唯一可行路径。

## 2. 决策摘要

| 维度 | 决策 |
|---|---|
| AS 位置 | **自托管极简 AS，内嵌 `mcp-coding-server`**（同时是 AS 与 RS），用 `mcp` SDK 的 `OAuthAuthorizationServerProvider`，不手搓加密 |
| 用户认证 | **单口令同意页**（passphrase，来自环境变量） |
| 持久化 | 签名密钥 + 已注册 client + 授权码/refresh token 落持久存储；部署/重启后**不必重新授权** |
| 作用域 | 单一作用域，认证通过 = 可用全部工具；写工具护栏仍是 ChatGPT 确认弹窗 + worktree 隔离 |
| 纵深防御 | **保留 Nginx 秘密路径**（攻击者需同时拿到秘密路径 + 有效 token，后者需口令） |

## 3. 架构与组件

在 `mcp-coding-server` 容器内新增一个 auth 模块（建议 `app/oauth/`），通过 SDK `FastMCP` 的 auth 配置挂载。组件：

1. **元数据发现端点**：`/.well-known/oauth-protected-resource`（RFC 9728，RS 指向 AS）、`/.well-known/oauth-authorization-server`（RFC 8414，声明端点 + 仅 S256 PKCE）。
2. **DCR `/register`**（RFC 7591）：ChatGPT 自注册为 public client（无 secret，强制 PKCE），写入持久存储。
3. **`/authorize`**：校验 client_id / redirect_uri / PKCE challenge → 显示单口令同意页 → 通过则签发绑定 PKCE 的授权码并跳回 redirect_uri。
4. **`/token`**：授权码 + PKCE verifier 换取签名 access token + refresh token（轮换）。
5. **RS token 校验**：除 `/healthz` 与 `/.well-known/*` 外，所有请求（streamable-http + 工具调用）强制校验 Bearer token。
6. **持久存储层**：存储接口 + 默认 **SQLite 实现**（stdlib `sqlite3`，挂卷，零新依赖）。

## 4. OAuth 端点与首次连接流程

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/healthz` | 免 | 健康检查（不变） |
| GET | `/.well-known/oauth-protected-resource` | 免 | RS 元数据，指向 AS |
| GET | `/.well-known/oauth-authorization-server` | 免 | AS 元数据 |
| POST | `/register` | 免 | DCR，返回 client_id |
| GET/POST | `/authorize` | 口令 | 同意页 + 签发授权码 |
| POST | `/token` | PKCE | 换 access/refresh token |
| (MCP) | streamable-http 端点 + 工具 | Bearer | 业务调用 |

```
ChatGPT →(秘密路径) /.well-known/oauth-protected-resource → 找到 AS
       → /.well-known/oauth-authorization-server → POST /register (DCR)
       → 浏览器打开 /authorize → 用户输口令 → 跳回带 code
       → POST /token (code + PKCE verifier) → 拿到 access + refresh token
       → 之后所有 MCP 调用带 Bearer token
```

## 5. 数据模型与持久化

- **签名密钥**：环境变量 `MCP_OAUTH_SIGNING_SECRET`；prod 下缺失或过短则拒绝启动。
- **持久存储**（`MCP_OAUTH_STORE_PATH`）：
  - `clients`：client_id → 注册元数据（redirect_uris 等）。
  - `auth_codes`：短期，绑定 client_id / redirect_uri / PKCE challenge / 过期时间，一次性。
  - `refresh_tokens`：用于轮换。
  - access token 用 JWT（HS256，签名密钥），**无状态、不入库**。
- 默认实现：**SQLite**（stdlib `sqlite3`），借事务保证原子写与并发安全，存储抽象在接口之后（便于将来替换）。运行时假设 uvicorn `worker=1`（与 `stateless_http=True` 配合）；多 worker 由 SQLite 事务兜底 —— 见第 13 节开放风险。

## 6. 单用户认证（/authorize）

- GET 渲染极简口令表单；POST 用 **constant-time 比较** 校验 `passphrase == MCP_OAUTH_PASSPHRASE`。
- 失败重试 + 简单限速（防爆破）。
- 成功后签发授权码，绑定 client / redirect / PKCE，跳回。

## 7. Token 校验（RS）

每个受保护请求校验 Bearer JWT：签名（HS256）、`aud`（= 本 RS / issuer）、`exp`。失败返回 `401` + `WWW-Authenticate`，其中携带 protected-resource 元数据地址，引导 ChatGPT 重走 OAuth 流程。access token 短 TTL；refresh token 轮换。

## 8. 配置、部署与依赖

**环境变量**（真实值只在 ECS `runtime.env`，**不进 git**；`runtime.env.example` 仅加占位）：

| 变量 | 说明 |
|---|---|
| `MCP_OAUTH_PASSPHRASE` | 同意页口令 |
| `MCP_OAUTH_SIGNING_SECRET` | JWT 签名密钥（≥32 字节） |
| `MCP_OAUTH_ISSUER` | 公网 issuer，= 完整秘密路径 URL（不入 git） |
| `MCP_OAUTH_STORE_PATH` | 持久存储文件路径（挂卷） |
| `MCP_OAUTH_ENABLED` | 灰度开关；prod 验证通过后必须为 on |
| `MCP_OAUTH_*_TTL` | access / refresh / code 过期时间（带默认） |

**Nginx**：秘密路径下需代理 `/.well-known/oauth-*`、`/authorize`、`/token`、`/register`，以及现有 MCP 端点与 `/healthz`。元数据中所有 URL 必须是 `MCP_OAUTH_ISSUER` 公网下的绝对地址。

**版本/CI**：`mcp-coding-server` 是独立容器，运行时可用所需 `mcp`/`starlette`。`starlette<0.48` 仅为**共享 CI venv** 而存在；若 `OAuthAuthorizationServerProvider` 需要更新的 starlette，则把 mcp-coding-server 的测试**隔离成独立 CI job/venv**，不动 backend 的 pin。实现前先核实可用的最低 `mcp` 版本与 starlette 兼容性。

## 9. 错误处理与降级

- 无/坏 token → `401` + 元数据指针。
- 坏 PKCE / 过期或重放 code → `400`。
- 错口令 → 同意页重试 + 限速。
- executor 不可用 → 现有优雅降级（`executor: unavailable`）不变。

## 10. 测试

- importlib 文件加载法单测（绕开 `app` 包名冲突，沿用现有模式）：元数据端点形状、DCR、authorize 口令通过/拒绝 + PKCE 绑定、token 兑换 + PKCE 校验、token 校验（有效/过期/错 aud/缺失 → 401）、`/healthz` 与 `/.well-known/*` 免鉴权。
- 全量 `discover` 复现共享 venv 坑。
- 可选：用 SDK client 模拟一条端到端 happy-path。

## 11. 人工步骤（运维红线）

- 选口令、生成签名密钥 —— **已完成生成**。
- ChatGPT 开发者模式 —— **已开**，连接器 "AliECS Coding" 已存在（当前 auth=无）。
- 部署后：写 ECS `runtime.env` → 改 Nginx → 重配 ChatGPT 连接器并走一次 OAuth 同意（auth 从「无」改为 OAuth；ChatGPT 可能需删旧连接器重建——计划阶段确认）。

## 12. 显式 out of scope

多用户 / RBAC、细粒度 scope、Phase 3b（无人值守 headless agent 循环）、Phase 5（webdock 自动点确认弹窗）。

## 13. 开放风险（计划阶段验证）

1. 支持 `OAuthAuthorizationServerProvider` 的最低 `mcp` 版本，与 `starlette<0.48` 的兼容性（可能触发 CI venv 隔离）。
2. `/.well-known/*` 的定位与秘密前缀的关系：ChatGPT 连接器如何发现 AS（是否需要域根级 well-known 指回带前缀的 AS）。
3. `stateless_http=True` + uvicorn worker 数 与持久存储并发模型。
4. ChatGPT 是否允许就地修改连接器 auth，还是必须删除重建。
