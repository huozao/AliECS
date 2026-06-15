# Couple Memory：启用 Immich 照片底座（2026-06-15）

## 现象

`https://www.hydwang.xyz/couple/` 横幅显示「Immich 未启用，当前使用本地/旧电脑照片通道」，但 Immich 已可经 Tailscale `http://100.97.176.57:2283` 访问。

## 根因（分层，非单一开关）

横幅由 `/v1/immich/status` 驱动，`IMMICH_ENABLED=false` 时直接返回 `{enabled:false}`（`services/backend-api/app/immich_client.py`）。该集成是**有意关闭上线**的，因为从 ECS 后端到 Immich 的生产链路从未接通：

1. ECS 后端容器 `IMMICH_ENABLED=false`（横幅直接原因）。
2. `IMMICH_API_KEY` 为空。
3. `IMMICH_BASE_URL=https://immich.hydwang.xyz`：公网 DNS 解析到 ECS，但 **nginx 证书不覆盖该子域**，HTTPS 校验失败。
4. 可用的私有链路是反向 SSH 隧道（旧电脑 `webdock-immich-tunnel.service` 维持 `127.0.0.1:12283`，ECS 主机 `curl` 返回 `pong`），但 **后端容器到不了它**：容器经 `host.docker.internal`(=`172.17.0.1`) 访问宿主，而 Immich 隧道仅绑 `127.0.0.1:12283`，没有像 WebDock 照片(11800)那样的代理把它暴露到 docker 网关。
5. 用户看到的 Tailscale 地址来自其客户端，ECS 后端容器**不在 tailnet**（容器内 `curl 100.97.176.57:2283` 超时），与 ECS→Immich 链路无关。

`IMMICH_PROXY_MODE=backend`（浏览器→后端→Immich，浏览器不直连 Immich），因此 Immich **无需公网暴露**，采用私有隧道最稳妥、攻击面最小。

## 修复

1. **隧道代理（ECS 主机）**：新增 `immich-tunnel-proxy.service`，复用通用 `webdock-tunnel-proxy.py`，监听 `172.17.0.1:12283` → 转发 `127.0.0.1:12283`。容器内 `host.docker.internal:12283/api/server/ping` 已返回 `pong`。
2. **后端 env**：`IMMICH_ENABLED=true`、`IMMICH_BASE_URL=http://host.docker.internal:12283`、`IMMICH_API_KEY=<runtime secret>`、`IMMICH_PROXY_MODE=backend`。重建 `ecs-backend-api-1` 后 `ImmichClient().status()` = `{"enabled": true, "ok": true, "detail": "ok"}`。
3. **持久化（本 PR）**：`deploy.sh` 每次部署会用 heredoc 重新生成 `runtime.env`，原本无 `IMMICH_*` → 部署会冲掉手改。本 PR 给 `deploy.sh` 增加 `IMMICH_*` 默认值与 heredoc 模板、以及 `IMMICH_TUNNEL_PROXY_ENABLED` 安装钩子；真实值（含 API key）写入 ECS 私有 `release-meta.env`（不入库）。

## 选片代码

`/couple/` 的 Immich 选片闭环已由 #113 实现并部署（`GET /v1/immich/assets` 搜索、`GET /v1/immich/assets/{id}/thumbnail` 后端代理、`POST/GET/DELETE /v1/memories/{id}/immich-assets` 绑定）。本次仅补运行时接通 + 持久化，无需改业务代码。

## 待人工 / 后续

- `STORAGE_DRIVER` 仍为 `local`（上传落 ECS 本地磁盘）。要切到旧电脑（webdock），需先在 `release-meta.env` 配 `WEBDOCK_PHOTO_API_TOKEN`（与 webdock `/storage/photos` 的 token 一致），否则 `deploy.sh` 会拒绝 `STORAGE_DRIVER=webdock`。
- 公网 `immich.hydwang.xyz` 的 TLS 证书未覆盖该子域；私有隧道方案下无需修复，除非将来要公网直连 Immich。

## 验证

- `bash -n deploy/ecs/deploy.sh`、`bash -n deploy/ecs/install-immich-tunnel-proxy.sh`。
- `docker compose --env-file deploy/ecs/runtime.env.example -f deploy/ecs/compose.prod.yml config`。
- 线上：`ecs-backend-api-1` healthy；`/v1/immich/status` → `{enabled:true, ok:true}`；`https://www.hydwang.xyz/couple/` = 200。
