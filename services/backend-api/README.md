# backend-api

FastAPI service for AliECS. This is the public API boundary for authentication, RBAC, admin operations, webhook gateway, doc-sync queries, recipe query, and downloadable workbooks.

## Runtime Inputs

- Postgres through `DATABASE_URL`.
- Auth and bootstrap env vars such as `AUTH_TOKEN_SECRET` and `ADMIN_BOOTSTRAP_*`.
- Chanjet webhook env vars such as `CHANJET_APP_KEY`, `CHANJET_APP_SECRET`, and `CHANJET_WEBHOOK_AES_KEY`.
- T+ worker output mounted read-only at `RECIPE_BOM_INPUT_DIR`, default `/app/tplus-output/excel`.
- `CLASH_SELF_NODES_B64` for the Clash profile composer: **base64 of** a JSON array of complete
  clash proxy definitions, each requiring at least `name` and `server`. Managed in SOPS and
  rendered at deploy time — never commit real node parameters, this repository is public.
  Base64 rather than raw JSON is deliberate: `deploy/ecs/deploy.sh` loads the runtime env with
  `set -a; source <file>`, and bash quote removal turns `[{"name":"a"}]` into `[{name:a}]`
  (measured 2026-08-11). The value crosses four layers with different quoting rules — bash
  source, heredoc expansion, dotenv, compose interpolation — and the base64 alphabet needs no
  escaping in any of them. When the variable is missing, not valid base64/UTF-8, not valid JSON
  after decoding, empty, or holds an element without `name`/`server`, both
  `/v1/admin/clash-profile/preview` and `/download` return 500 with an explanatory message rather
  than emitting a profile that would fail to start on the client.
- 可选 `CLASH_TAILSCALE_B64`：base64 编码的 JSON 对象，含 `hostname`、`auth-key` 和
  `hosts`（主机名到 IP 的映射）。真实登录密钥、设备名和地址由 infra SOPS 管理，不能进入
  此公开仓库。缺失或格式错误时，仅请求内置 Tailscale 的下载返回 503，普通版本仍可用。
  环境变量为空时也支持 `/run/secrets/clash-tailscale.b64`，内容为相同 base64 值，由 SOPS
  解密渲染并限制为 0600；用于 Docker secret 或已授权热补丁。显式无效环境值不会回退到文件。

## Runtime Outputs

- API responses for public-web and admin-ui.
- Clash profile composer under `/v1/admin/clash-profile` (all admin-only): `GET/POST /providers`,
  `PUT/DELETE /providers/{id}` manage third-party subscription sources stored in
  `clash_profile_providers`; `GET /preview` returns the composed profile as plain text and
  `GET /download` returns it as an attachment. 订阅由服务端拉取并保存快照；desktop/WebDock
  使用另行下载的 provider 文件，mobile 将所有启用源的快照节点嵌入一个 YAML。
- 手机内置 Tailscale：管理员在页面点击“下载手机配置（含 Tailscale）”，调用
  `/preview?target=mobile&tailscale=true`；`/download` 支持相同参数并返回
  `clash-profile-mobile-tailscale.yaml`。两个接口均要求管理员权限，并返回 `Cache-Control: no-store`。
  仅手机目标允许 `tailscale=true`，其他目标返回 422。文件保留机场与自建节点、策略组和分流，
  同时加入独立 Tailscale 节点；该节点不加入公网出口选择组。`100.64.0.0/10`、Tailscale IPv6
  网段、`*.ts.net` 及配置的主机名都送入此节点。手机客户端须支持 mihomo 的 `type: tailscale`，
  不需要另开独立 Tailscale VPN。`hosts` 仅解决地址映射，SSH 端口、用户名、公钥仍由各设备决定。
  初次接入可能需要等待节点登录；下载文件含入网密钥，应私密保存。
- Postgres writes for auth/RBAC, features, sync requests, webhook state, and admin operations.
- Temporary recipe query workbooks under `RECIPE_EXPORT_DIR`.
- Chanjet event spool files under `CHANJET_EVENT_SPOOL_DIR`.

## Do Not Commit

- Real `.env` files, runtime env files, tokens, secrets, webhook tickets, generated workbooks, spool files, logs, or business data.
- Absolute local paths from one machine unless they are only used in tests.

## Validation

Clash 相关离线回归（仅合成节点与密钥，不连接生产库）：

```bash
python -m pytest tests/test_clash_profile_render.py tests/test_clash_profile_router.py tests/test_admin_frontend.py
```

授权部署时，把私有 JSON 编码后写入 infra SOPS 对应运行 profile 的 `CLASH_TAILSCALE_B64`，
再走既有渲染与部署流程；Compose 与 deploy 白名单已包含此变量。仅修改配置示例不会配置
真实入网密钥。回退时使用普通手机下载入口；不改变设备 SSH 或 Tailscale 服务。

2026-10-24 手机验收：用户确认只开启手机 Clash，即可用 Termius 分别操作两台受控设备的
终端，不依赖独立 Tailscale VPN。此结论来自用户实测；更换客户端或重新入网后的状态需重新
验证。部署后还应确认下载接口使用正式镜像与环境变量，而不是仅依赖容器写层的热补丁文件。

```powershell
python -m compileall AliECS\services\backend-api\app
python -m unittest AliECS.tests.test_backend_recipes
```

When Docker is available:

```powershell
docker compose -f AliECS\local\docker-compose.local.yml config
curl.exe -fsS http://127.0.0.1:8000/healthz
```
