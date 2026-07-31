# 设备清单（Fleet）

> 单一事实源：设备 ↔ 职责 ↔ 仓库 ↔ 部署链路的权威映射。
> 基础事实于 2026-07-04 经 SSH 实测核实；腾讯云并行迁移状态于
> 2026-07-26 复核。发现与本文不符时，以实测为准并更新本文。

## 命名规则

- 逻辑名 = 角色 + 序号，小写字母数字（如 `webdock1`、`webdock2`），**不用硬件序列号**。
- 逻辑名跟角色走：换硬件接替同一职责时逻辑名不变，只更新档案里的硬件字段。
- 同一个逻辑名用作 ssh alias、tailscale 机器名，保持一致；历史叫法登记进"别名"字段。
- 业务代码不硬编码设备名（已核实：两仓库代码零命中）。将来若代码需区分实例，用环境变量（如 `DEVICE_NAME=webdock2`）注入。

## 设备总表

| 逻辑名 | 角色 | 硬件/OS | 网络入口 | 运行代码来源 |
|---|---|---|---|---|
| `devbox` | 开发机 | 本地 Windows 11 | 本机 | 3 个仓库克隆（不运行生产） |
| `aliecs` | 海外边界 + business-cn 隔离候选 | 阿里云 ECS 美国 / Ubuntu 24.04，2G 内存 | `ssh aliecs`（root@47.77.176.62） | console/ERP；旧写端冻结；可作反向迁移候选 |
| `txecs` | 当前生产唯一写端 / business-cn 主栈 / 公网中心边界 | 腾讯云轻量 / Ubuntu 24.04，4C4G | `ssh txecs`（ubuntu@106.52.51.67） | 主站、PostgreSQL、SSO、OpenClaw、bridge、worker、nginx |
| `webdock1` | webdock 算力节点（**当前备用**） | 旧 Ubuntu 笔记本 | `ssh webdock1`（Tailscale 100.97.176.57） | webdock 镜像 + 第三方自托管服务 |
| `webdock2` | webdock 算力节点（**当前主力**） | 新台式机 Windows 11 + WSL2 | `ssh webdock2`（Tailscale 100.67.38.52） | webdock 镜像 |

### 当前事实与目标定位

- **当前事实**：txecs 是生产唯一写端；三个 worker（doc-sync、T+ 只读同步、T+ 写回）
  只在 txecs 的 `business-cn-*` 实例运行，aliecs 的旧 `ecs-*` 实例保持停止。
- **目标定位**：txecs 与 aliecs 互为恢复目标，允许未来向任一方向迁移。
- **尚未证明**：角色仍含设备命名和非对称能力，数据库冻结、DNS、SSO、ERP、
  ProductCenter、WebDock 与密钥恢复尚未完成一次完整反向演练，因此不能宣称可在
  20–40 分钟内双向切换。

## webdock 主备路由（关键，勿凭猜测）

```
bridge/openclaw 容器
  → 172.17.0.1:11800   webdock-tunnel-proxy（容器侧转发）
  → 127.0.0.1:11800    webdock-failover-proxy（主备切换）
      ├─ 主 127.0.0.1:11810 ← webdock2 反向隧道（2026-07-02 起）
      └─ 备 127.0.0.1:11811 ← webdock1 反向隧道
```

- **主备关系的唯一权威来源是当前 business-cn 主机（现为 txecs）上的 `/etc/default/webdock-failover-proxy` 与 `127.0.0.1:11800/healthz` 响应头**。当前 primary=webdock2、standby=webdock1。⚠️ 端口只是实现细节：当前 primary 端口为 11810、standby 端口为 11811；切换服务器或主备时不得只凭端口号判断。
- 主上游失败自动切备（标记 60s），并给回复加"已自动切换备用服务器"前缀。
- 代理在响应头标注实际来源：`X-Webdock-Device`（设备名，来自环境文件 `WEBDOCK_FAILOVER_PRIMARY_NAME`/`STANDBY_NAME`）和 `X-Webdock-Route`（primary/standby）；bridge 用它渲染飞书卡片灰色脚注（例如 `设备: webdock2(主) | 项目: xx | 耗时: Ns`，以实际响应头为准）。
- 切换主备 = 改该环境文件里的 `PRIMARY_*` / `STANDBY_*`（尤其是 `*_NAME` 与对应 host/port 绑定）后 `systemctl restart webdock-failover-proxy`，不改代码。
- 判定某条消息由哪台处理：查各机 `/var/log/webdock/archive/<UTC日期>.jsonl`（权威记录）。

### 腾讯云当前生产路径

```
webdock2 WSL
  ├─ 已停旧业务隧道 → aliecs 127.0.0.1:11810
  ├─ console 隧道   → aliecs 127.0.0.1:16090/16091
  └─ 生产业务隧道   → txecs 127.0.0.1:11810

txecs 127.0.0.1:11800 failover-proxy
  ├─ 主 127.0.0.1:11810 ← webdock2（已验证）
  └─ 备 127.0.0.1:11811 ← webdock1（设备离线，待验证）
```

- 当前生产 bridge 已迁 txecs，并经 txecs `127.0.0.1:11800` 访问
  webdock2 主力；AliECS 原 bridge/gateway 保持停止。
- 两个落点的 11810 是不同主机上的回环端口，不冲突。
- txecs 主备权威由 txecs `/etc/default/webdock-failover-proxy` 和
  `11800/healthz` 响应头共同确认；不得拿 AliECS 的环境文件判断 txecs。
- WebDock browser_data 不随业务服务器迁移；console 隧道仍落 AliECS。

## 设备档案

### aliecs（海外边界与 business-cn 隔离候选）

- 别名：服务器。
- 入口：`ssh aliecs`（root@47.77.176.62）。ECS **不在 tailnet**。
- 当前运行容器包括旧 public-web/admin-ui/postgres（只作回滚）和
  mcp-coding-server；旧 SSO、业务写端、worker、OpenClaw 和 bridge 均停止。
- 公网 Nginx 只启用 console、ERP 和默认拒绝；其他历史 server 块隔离在
  `/etc/nginx/conf.inactive.d/`。console 的 forward-auth 在 txecs 完成，
  AliECS 源站只允许 txecs 访问。
- 反向迁移候选使用独立 `/srv/business-cn` 和 `business-cn-*` 容器。
  在线准备阶段只允许空 PostgreSQL，不启动业务/worker，不覆盖 console/ERP。
- 隧道端口（127.0.0.1）：11800 为 webdock failover 入口；当前 11810←webdock2 主、11811←webdock1 备（以 `/etc/default/webdock-failover-proxy` 的 NAME 绑定为准）；12283←webdock1 Immich、18015/18016←webdock1 AdventureLog，另有 Gokapi/Authentik 隧道（端口见 webdock1 各 unit 的 env）。
- 远程控制台（2026-07-04，`https://hydwang.xyz/console/`）：nginx `/console/*` 七路 location（认证=Authelia `two_factor` + lldap `console_admins` 组，成对 deny 兜底；**VNC 层免密设计**，2FA 是唯一闸门）；本机组件 ttyd 7681（unit `ttyd-console`，⚠️ apt 自带 `ttyd.service` 抢端口须 disable）、webtop 3000 按需启停（`/opt/aliecs/aliecs-temp-desktop.sh`，2G 内存用完必须 stop）；ECS `authorized_keys` permitlisten 新增四条 160xx（16080/16081←webdock1、16090/16091←webdock2，与生产 118xx 隔离）。详见 infra `console/README.md`。
- 部署：push AliECS main → release-deploy 自动构建部署业务镜像；bridge 镜像同流程构建，且当 `deploy/openclaw-bridge/**` 的内容树 hash 变了时，release-deploy 构建完自动调用 `bridge-cutover`（自动解析 digest、验证并失败回滚）。bridge 没变的合并完全不碰它；回滚/重切/切非 main ref 仍走 `bridge-cutover` 的手工 `workflow_dispatch`（填 `CUTOVER_TXECS`）。
- 排障：`docker ps`、bridge 日志 `docker logs openclaw-bridge`、部署尖峰时 health 告警多为瞬时（2G 内存超卖）。

### txecs（腾讯云 business-cn 生产主栈）

- 入口：`ssh txecs`（`ubuntu@106.52.51.67`）；系统 Ubuntu 24.04，
  4 vCPU、约 3.6 GiB 可用内存。
- 当前是生产唯一写端；主站、backend、OpenClaw、bridge 和已启用 worker
  均从本机运行。
- 2026-07-31 已确认 `business-cn-doc-sync-worker-1`、
  `business-cn-tplus-sync-worker-1`、`business-cn-tplus-write-worker-1` 均运行，
  `restart=always`、RestartCount=0；aliecs 同类旧实例停止，避免双写。
- 主机重建入口：infra
  `roles/server/{common,tencent}`；age 私钥和 GitHub infra 只读 deploy
  key 是最小人工输入。
- 应用部署：AliECS `release-deploy` 的独立 `business-cn` job；
  `/srv/business-cn/current` 记录当前源码提交，镜像从 TCR 按 digest 拉取。
- WebDock：`127.0.0.1:11800` 为 failover 入口，
  `11810←webdock2` 已验证，`11811←webdock1` 待设备上线。
- 公网边界：UFW 开放 80/443；Nginx 默认站点仍返回 444。`@`/`www`、
  `auth`/`lldap` 均在本机终止 TLS，Authelia/LLDAP 也在本机运行。无
  sing-box、mihomo 或任何第三方出海转发能力。
- 排障：
  `sudo systemctl status webdock-failover-proxy`、
  `curl -i http://127.0.0.1:11800/healthz`、
  `readlink -f /srv/business-cn/current`。

### webdock1（旧笔记本，当前备用）

- 别名：旧电脑；ssh alias `webdock` / `webdock1` / `WebDock01`；tailscale/hostname `webdock-laptop`；用户 `webdock`。
- 运行：`webdock` 容器（ghcr.io/huozao/webdock:sha-xxx）+ Chrome/ChatGPT 登录态（browser_data 卷，**登录必须人工做，红线**）、noVNC `http://100.97.176.57:6080/`、Immich(2283)、AdventureLog(8015/8016)、Gokapi、Authentik。
- 反向隧道 unit（systemd）：`-R 11811`（webdock API，备）、`-R 12283`（Immich）、`-R 18015/18016`（AdventureLog）、Gokapi/Authentik（env 参数化）。
- 远程控制台：x11vnc :5900（`-nopw`，回环）+ noVNC/websockify 6081（**只绑 Tailscale IP** 100.97.176.57）+ `console-ecs-tunnel`（`-R 16080`←容器 noVNC 6080、`-R 16081`←桌面 6081）；unit：`x11vnc-desktop` / `novnc-desktop` / `console-ecs-tunnel`；GDM 已切 Xorg（`WaylandEnable=false`，自动登录）。
- 部署：拉新镜像 + `systemctl restart webdock`（卷不丢登录态）。webdock 仓库小改可直推 main（**直推前本地 pytest**，CI 不跑直推）。
- 日志：消息存档 `/var/log/webdock/archive/<UTC日期>.jsonl`（收发全文+lane+status）；容器内 `/app/logs/`。
- 验证：`ssh webdock1 'curl -fsS http://100.97.176.57:18000/healthz'`。
- 硬件注意：合盖不挂起已配好（可当服务器）；断电史见运维记忆，建议 BIOS 来电自启。

### webdock2（新台式机，当前主力）

- 别名：新电脑、desktop；ssh alias `desktop` / `webdock2` / `WebDock02`；Windows 主机名 `DESKTOP-D0LV1TN`；用户 `Admin`。
- 结构：**SSH 登录进的是 Windows（PowerShell）**，WebDock 跑在 WSL2 发行版 `Ubuntu-24.04-WebDock` 内的 docker 里。Linux 命令一律 `ssh webdock2 "wsl -d Ubuntu-24.04-WebDock -- <cmd>"`。
- 运行：仅 `webdock` 容器（与 webdock1 同镜像同 tag）。Immich / AdventureLog / Gokapi 暂不部署（按需再拉）。
- 反向隧道：WSL 内 `webdock-business-tunnel` 将 WebDock 18000 映射到
  txecs 11810（当前生产）；旧 `webdock-ecs-tunnel` 已停，console 仍单独
  落 AliECS。
- 远程控制台：Windows TightVNC :5900 服务模式（`UseVncAuthentication=0` 免密，防火墙只放行 172.16.0.0/12+100.64.0.0/10，MSI 自建全放行规则已 Disable）+ WSL noVNC 6091（`novnc-desktop`，启动时动态解析 WSL 网关）+ WSL `console-ecs-tunnel`（`-R 16090`←容器 noVNC 6080、`-R 16091`←桌面 6091）。
- noVNC：`http://100.67.38.52:6080/` 可用。⚠️ 已知怪癖：Tailscale 直连 `100.67.38.52:18000` 返回 502（Windows→WSL 端口转发问题），**生产链路不受影响**（隧道从 WSL 内 localhost 拉出）；从 ECS 探 `127.0.0.1:11810/healthz` 才是有效健康检查。
- 日志：WSL 内 `/var/log/webdock/archive/`。

### devbox（开发机）

- 别名：开发机、本机。Windows 11，工作区 `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock`。
- 持有：AliECS、webdock、infra 三个仓库克隆；`gh` 已认证（nihil7）；`~/.ssh/config` 定义全部设备别名。
- 不运行生产代码。

## 仓库 ↔ 设备映射

| GitHub 仓库 | 部署到 | 部署链路 |
|---|---|---|
| `huozao/AliECS` | aliecs + txecs | push main → 构建并同步 GHCR/TCR；txecs `business-cn` 为生产 job，AliECS `business-candidate` 只预拉同一镜像并启动空 PostgreSQL；bridge 已运行在 txecs；改动走 PR |
| `huozao/webdock` | webdock1 + webdock2 | CI 构建 sha-tag 镜像 → 各机手动拉取重启；两机应保持同 tag；小改可直推 main |
| `huozao/infra`（私有） | aliecs + txecs + webdock1/2 主机层 | 角色、SOPS、nginx、隧道、bridge/edge 配置；在线设备按各自 remote/deploy key 同步 |

不属于任何仓库：openclaw 网关（上游镜像 + `/root/.openclaw` 主机配置）。
⚠️ `huozao/CatGPT-Gateway` 是废弃旧 fork，**不是** bridge 源码，勿混淆。

铁律：任何热补丁（docker cp、容器内改、runtime 配置改）验证成功后**必须回灌 git**，否则下次 release 重建镜像会覆盖丢失。

## 环境变量与密钥统一（2026-07-02 起）

全部设备的 env/密钥以 **infra 仓库 `secrets/`（SOPS+age 加密，键名明文可读）** 为单一事实源，非密钥配置在 `infra/config/` 明文。详见 `infra/secrets/README.md`。

- 每台主机一把 age 私钥（`~/.config/sops/age/keys.txt`），只解自己的文件；devbox 私钥可解全部（编辑入口）。
- 改密钥 = devbox 上 `sops set` → commit/push infra → 目标设备 `git pull && ./scripts/render.sh <设备名>` → 按提示重建容器。
- **`release-meta.env` / bridge `webdock.env` / openclaw `.env` / webdock 两节点 `.env` 都是渲染产物，不要在主机上直接长期修改**（应急热改后必须回灌 sops 源，否则下次 render 覆盖）。
- 设备侧同步通道：各机本地 bare 仓（aliecs `/root/infra.git`、webdock1/2 `~/infra.git`），由 devbox `git push device-*` 推送（设备未配 GitHub 私仓访问；三把备用 deploy 公钥已生成在各机 `~/.ssh/github_infra_deploy.pub`，需要时人工添加）。
- aliecs `/root/infra` 自 2026-07-02 起是真 git 克隆（旧手工拷贝备份在 `/root/infra.legacy-20260702`）。
- bridge 换镜像：改 `deploy/openclaw-bridge/**` 并合入 main 即自动 cutover（release-deploy 构建后调用 `bridge-cutover`，失败自动回滚）；回滚/重切用 `bridge-cutover` 的手工 `workflow_dispatch`。运行状态在 `/root/infra/server/release.env`；禁止登机手改版本。

## 新增设备流程

权威剧本：infra 仓 `roles/webdock/README.md`（机型化装机，2026-07-19 起；按机型 linux-native /
windows-wsl 分叉，设备参数集中在 infra `config/devices/<name>.env`）。本文只保留原则：

1. 逻辑名=角色+序号；tailscale 机器名、ssh alias 与逻辑名一致（写入 devbox `~/.ssh/config`）。
2. 端口分配查 infra `roles/webdock/README.md` 端口分配表，先登记后使用（16101=devbox 已占）。
3. 装机完成后：本文档"设备档案"按 7 项模板补一节（别名 / 硬件与OS / 运行什么 / 隧道与端口 /
   部署方式 / 日志位置 / 验证命令）+ 更新"设备总表" + 工作区根 `AGENTS.md` 简表。
4. 若接入 webdock 主备池：按需更新 `/etc/default/webdock-failover-proxy`（含 `*_NAME` 设备名两行）。
   ⚠️ 池仅 PRIMARY/STANDBY 两槽，且 proxy 脚本目前不在 git（infra spec 2026-07-18 §10 记录在案）。
5. ⚠️ webdock 节点必须初始化 `browser_data/runtime.json`（从现有节点复制：`media_base_url` +
   三个超时参数）。缺 `media_base_url` 时飞书出图/表格截图整条链路静默失效（2026-07-02 webdock2 踩坑）。
   ChatGPT 登录必须人工（红线）。

## 已知遗留问题（记录未处理）

- webdock2 Tailscale 直连 18000 返回 502（仅影响外部直连调试）。
- MEDIA 图片 token 存储在生成它的那台 webdock 本机：主备切换后，切换前发出的旧图片链接会 404（图片链接本就短期使用，暂接受）。
