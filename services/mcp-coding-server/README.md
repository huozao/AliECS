# MCP Coding Server

## OAuth（阶段四）

`MCP_OAUTH_ENABLED` 默认 `false`，保持现有无 OAuth 行为。生产开启前，真实值只写入 ECS 的 `runtime.env`，不要提交到 Git。

| 变量 | 用途 |
| --- | --- |
| `MCP_OAUTH_ENABLED` | OAuth 灰度开关；默认 `false`，上线验证后置 `true`。 |
| `MCP_OAUTH_ISSUER` | 秘密路径公网完整 URL，必须等于 ChatGPT 连接器使用的 MCP URL 根路径。 |
| `MCP_OAUTH_PASSPHRASE` | 自托管同意页口令；只放 ECS runtime env。 |
| `MCP_OAUTH_SIGNING_SECRET` | token/code 哈希 pepper；只放 ECS runtime env。 |
| `MCP_OAUTH_STORE_PATH` | SQLite 持久化路径，建议 `/data/oauth/oauth.db`。 |
| `MCP_OAUTH_ACCESS_TTL` | access token TTL，默认 `3600` 秒。 |
| `MCP_OAUTH_REFRESH_TTL` | refresh token TTL，默认 `2592000` 秒。 |
| `MCP_OAUTH_CODE_TTL` | authorization code TTL，默认 `600` 秒。 |

`MCP_OAUTH_STORE_PATH` 必须挂持久化卷，否则服务重建后 ChatGPT 需要重新授权。建议在 `compose.prod.yml` 给 `mcp-coding-server` 增加 named volume，并挂载到 `/data/oauth`。

Nginx 需要在同一个秘密路径下代理现有 MCP 路由、`/healthz`、`/.well-known/oauth-authorization-server`、`/.well-known/oauth-protected-resource...`、`/authorize`、`/token`、`/register`、`/revoke`、`/oauth/consent`。当 issuer 带路径时，SDK 会按 resource URL 派生 protected-resource metadata，例如 `https://host/mcp-x` 对应 `/.well-known/oauth-protected-resource/mcp-x`。

### 人工上线步骤

1. ECS `runtime.env` 填入 `MCP_OAUTH_PASSPHRASE`、`MCP_OAUTH_SIGNING_SECRET`、`MCP_OAUTH_ISSUER`（秘密路径 URL）、`MCP_OAUTH_STORE_PATH=/data/oauth/oauth.db`；先保持 `MCP_OAUTH_ENABLED=false` 部署一次，确认无回归。
2. `compose.prod.yml` 给 `mcp-coding-server` 挂 `/data/oauth` 卷；Nginx 增补上述路由代理；reload。
3. 置 `MCP_OAUTH_ENABLED=true` 部署；浏览器访问 `issuer + /.well-known/oauth-authorization-server` 应见 JSON。
4. ChatGPT 连接器当前 auth=无时，可能需先取消关联，再用同一 URL 重新添加，使其走 OAuth；浏览器弹同意页时输入口令。
5. 验证 `server_info` / `ping` 仍可用（已带 token）。再考虑接入写工具。
