# AdventureLog 接管 couple「地图+相册」设计规格

> 日期：2026-06-14。状态：设计已与用户逐项确认，待用户复核本文档后转实施计划。
> 一句话：在旧电脑上以独立 compose 栈部署 AdventureLog（旅行足迹 + Immich 相册），经 SSH 反向隧道 + ECS nginx 两个子域名对外；**自建 couple 的纪念日/恋爱天数/愿望清单/回忆全部保留**，仅把「地图足迹 + 照片相册」交给 AdventureLog，两者并存互链；一次性迁移现有带坐标回忆进 AdventureLog。

## 1. 决策汇总（已确认）

| 维度 | 决策 |
|---|---|
| 改造范围 | AdventureLog **接管 地图足迹 + Immich 相册**；自建 couple 其余模块（恋爱天数 KPI / 纪念日倒计时 / 愿望清单 / 回忆列表+详情 / dashboard）**保留并继续按 `COUPLE-FEATURE-DESIGN.md` 完善**；两者并存、互相链接 |
| 部署位置 | **旧电脑**（与 Immich、webdock 同机），独立 compose 栈，不进 ECS（避开 2G OOM） |
| 对外暴露 | **方案A：SSH 反向隧道 + ECS nginx**（复用 webdock 11800 / executor 18091 同款拓扑） |
| 域名 | 前端 `adventure.hydwang.xyz`；后端/媒体 `adventure-media.hydwang.xyz`（SPA 生产反代需两个子域名） |
| 认证 | AdventureLog 自带 AllAuth 登录；**关公开注册**；建 1 个 Django 超管 + 你俩 2 个用户；**不接入门户 token/SSO** |
| Immich | 每个 AdventureLog 账号在其设置里填 Immich URL + 个人 API key，从现有 Immich **拉图不复制**；Immich 仍是底层图库（共存） |
| 旧数据 | **一次性迁移**：现有带坐标 `memories` → AdventureLog adventures；照片关联 Immich；旧 `/map/` 退役（跳转 AdventureLog） |
| 许可证 | AdventureLog GPLv3，自托管不改源、不分发 → 无问题 |

## 2. 组件与边界

### 2.1 旧电脑新增 compose 栈 `adventurelog`（3 容器，自带库，独立于 Immich/AliECS）
| 服务 | 镜像 | 端口(host→container) | 说明 |
|---|---|---|---|
| `adventurelog-frontend` | `ghcr.io/seanmorley15/adventurelog-frontend:latest`（**pin 到具体 tag/sha**，勿用 latest 漂移） | `8015→3000` | SvelteKit SPA |
| `adventurelog-backend` | `ghcr.io/seanmorley15/adventurelog-backend:latest`（同上 pin） | `8016→80` | Django + DRF + AllAuth |
| `adventurelog-db` | `postgis/postgis:16-3.5` | 内部 | PostGIS，**独立卷**，不碰 AliECS Postgres |

- 与 Immich 同机但各自独立 compose、独立网络、独立卷。AdventureLog 经 **Immich 的公网域** `https://immich.hydwang.xyz/api` 取图（文档要求用真实域名/IP，不要 localhost）。
- 镜像 tag/sha 锁版本（与 infra 现有第三方镜像锁版本风格一致）。

### 2.2 ECS 只加「边」，不跑容器
- nginx 两个 server 块（两子域名，TLS 走现有 certbot）。
- 一个 systemd unit 维持反向隧道到旧电脑（见 §4）。
- **ECS 不部署任何 AdventureLog 容器**。

### 2.3 自建 couple（保留）
- `backend-api` 的 memories/anniversaries/wishlist/photos 端点、`public-web` 的 `/couple//memories/` 等**原样保留**。
- 仅 `/map/` 退役（§6）。

## 3. 关键环境变量（生产·两子域名）

> AdventureLog 版本会演进，**实施时以该版本 `.env.example` 为准核对变量名**；下面是依据其反代文档/社区两子域名范例的目标值。

**frontend (web) .env**
```
ORIGIN=https://adventure.hydwang.xyz
PUBLIC_SERVER_URL=https://adventure-media.hydwang.xyz
FRONTEND_PORT=8015
BODY_SIZE_LIMIT=<按上传需求, 如 100M>
```
**backend (server) .env**
```
PUBLIC_URL=https://adventure-media.hydwang.xyz
CSRF_TRUSTED_ORIGINS=https://adventure.hydwang.xyz,https://adventure-media.hydwang.xyz
BACKEND_PORT=8016
DEBUG=False
SECRET_KEY=<强随机, 入旧电脑本地 secrets, 不进 git>
DJANGO_ADMIN_USERNAME=<超管>
DJANGO_ADMIN_PASSWORD=<强口令, 不进 git>
DJANGO_ADMIN_EMAIL=<邮箱>
# 关注册: 核对该版本的注册开关 env(如 DISABLE_REGISTRATION) ;
# 若无此 env, 首次启动后到 Django admin / 应用设置里关闭公开注册, 并在 ops 文档记录
```
**db .env**
```
POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD=<强口令, 不进 git>
PGHOST=db
```

## 4. 暴露拓扑（方案A：SSH 反向隧道）

```
浏览器 ──TLS──▶ ECS nginx
   ├─ adventure.hydwang.xyz        ──▶ 127.0.0.1:18015 ─┐
   └─ adventure-media.hydwang.xyz  ──▶ 127.0.0.1:18016 ─┤  反向SSH隧道
                                                         ▼
                                          旧电脑 8015(前端) / 8016(后端)
```

- 旧电脑一个 autossh systemd unit：`ssh -R 18015:localhost:8015 -R 18016:localhost:8016 aliecs`（隧道端口 18015/18016 当前空闲，避开 webdock 11800 / executor 18091 / openclaw 18789）。开启 SSH keepalive（参照已修的 `10-tunnel-keepalive.conf` 经验，防掉线占端口）。
- ECS nginx 两个 `server{}`：`server_name` 各一子域名，`proxy_pass http://127.0.0.1:1801x;`，带 `proxy_set_header Host/X-Forwarded-Proto/X-Forwarded-For`、WebSocket upgrade 头（SvelteKit 需要），`client_max_body_size` 与 `BODY_SIZE_LIMIT` 对齐。
- 两子域名各签 TLS 证书（certbot）。
- ⚠️ 两条 nginx server 块属"宿主 nginx 配置、不在 git"（与现有 OAuth 路由同类脆弱点）——把两段 nginx 片段 + 隧道 unit 一并归档进 `infra` 仓 runbook，避免重建丢失。

## 5. 认证与访问

- AdventureLog 自带 AllAuth：建超管 + 你俩 2 用户；**关公开注册**（§3）。
- 公网可达，靠强口令 + 关注册兜底；可选在 ECS nginx 两子域加 `limit_req` 限流 / fail2ban。
- 与门户 token 体系**不打通**；couple ↔ AdventureLog 仅靠链接跳转（§6）。

## 6. couple 互链 + 旧 /map/ 退役

- `public-web/couple/index.html` dashboard 的「地图足迹 / 相册」卡片 → 改为外链 `https://adventure.hydwang.xyz`（新标签打开）。
- `public-web/map/index.html` → 退役：保留路由但页面改为「已迁移」提示 + 自动跳转 AdventureLog（防旧书签 404）。其 `/v1/map/memories` 后端端点可保留备迁移用，迁移完成后由后续决定是否下线。
- couple 其余模块（恋爱天数 / 纪念日 / 愿望清单 / 回忆列表+详情 / 分享）继续按 `COUPLE-FEATURE-DESIGN.md` 推进，**不受本设计影响**。

## 7. 一次性数据迁移（idempotent ETL）

- 形式：旧电脑本地跑的一次性脚本（Python），读 AliECS Postgres，调 AdventureLog REST API 写入。
- 源：`memories`（取 `lat/lng` 非空者）→ 字段映射 title→name、place_name→location、lat/lng→坐标、memory_date→访问日期、content→描述、tags→AdventureLog 标签/分类、visibility→公开/私有。
- 照片：`photos` 表若已是 Immich 资产（`storage_driver`/`external_*` 指向 Immich）→ 在 AdventureLog 里按 Immich asset 关联；仅在 webdock 本地的 → 先入 Immich 或作为 AdventureLog 上传图附上（迁移脚本对这类逐条记录到对账报告，必要时人工补）。
- 幂等：以源 `memories.id` 作幂等键（写进 AdventureLog adventure 的某外部引用字段/描述标记），重复跑不重复建。
- 产出：`docs/ops/adventurelog-migration-report-2026-06-14.md`（成功/跳过/需人工 的逐条对账）。
- 鉴权：脚本以一个 AdventureLog 用户的 API token 调用；token 不进 git。

## 8. 资源与运维（旧电脑）

- 旧电脑已跑 Immich + webdock(Chromium/Xvfb/x11vnc)。新增 AdventureLog 前**先核内存余量**；给 `adventurelog-db` 与 `adventurelog-backend` 设合理 `mem_limit`，避免与 Immich/webdock 争内存。
- 备份：把 AdventureLog 的 **PostGIS 数据卷 + media 卷**纳入现有 restic（与 Immich 备份策略一致；照片本体在 Immich，AdventureLog 主要备元数据库）。
- 镜像锁版本；升级走"先快照卷 → 拉新 tag → 验证 → 回退可摘"。

## 9. 测试与回退

**验收清单**：
1. 两子域名 TLS 正常、`adventure.` 能开 SPA、`adventure-media.` 后端可达。
2. 登录成功；公开注册已关（匿名访问注册被拒）。
3. 账号设置里连 Immich 成功、能搜到并挂上 Immich 相册图。
4. 迁移脚本跑完，抽样 3-5 条对得上（地点/坐标/日期/描述）。
5. `/couple/` 的地图/相册入口跳转正常；旧 `/map/` 跳转生效、无 404。
6. 旧电脑内存稳定（无 OOM）、隧道稳定（无掉线占端口）。

**回退**：`docker compose -f <adventurelog compose> down` + 摘掉 ECS nginx 两 server 块 + 停隧道 unit；自建 couple 因低耦合不受影响。

## 10. 不做（YAGNI）

- 门户 SSO/OIDC 接入 AllAuth。
- 把 anniversaries / wishlist / 恋爱天数 塞进 AdventureLog（保留在自建 couple）。
- 改 AdventureLog 源码 / 二次开发其前端。
- 行程协作分享给外部人、活动轨迹(GPX)等 AdventureLog 高级功能（先不启用，后续按需）。

## 11. 待实施时核对的不确定项

- AdventureLog 当前版本「关公开注册」的确切机制（env 还是 admin 设置）。
- 该版本 `.env.example` 的精确变量名（`PUBLIC_URL`/`PUBLIC_SERVER_URL`/`ORIGIN`/`CSRF_TRUSTED_ORIGINS` 命名）。
- AdventureLog REST API 创建 adventure / 关联 Immich 资产的确切端点与字段（迁移脚本据此对齐）。
- 旧电脑实际可用内存余量（决定 mem_limit 与是否需要给旧电脑加 swap）。
