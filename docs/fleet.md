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
| `aliecs` | 当前生产写端 + 海外边界 | 阿里云 ECS 美国 / Ubuntu 24.04，2G 内存 | `ssh aliecs`（root@47.77.176.62） | 当前业务全栈；P1 后只保留 edge-us |
| `txecs` | P0 隔离 business-cn 候选主栈 | 腾讯云轻量 / Ubuntu 24.04，4C4G | `ssh txecs`（ubuntu@106.52.51.67） | business-cn 隔离栈 + txecs 主机角色 |
| `webdock1` | webdock 算力节点（**当前备用**） | 旧 Ubuntu 笔记本 | `ssh webdock1`（Tailscale 100.97.176.57） | webdock 镜像 + 第三方自托管服务 |
| `webdock2` | webdock 算力节点（**当前主力**） | 新台式机 Windows 11 + WSL2 | `ssh webdock2`（Tailscale 100.67.38.52） | webdock 镜像 |

## webdock 主备路由（关键，勿凭猜测）

```
bridge/openclaw 容器
  → 172.17.0.1:11800   webdock-tunnel-proxy（容器侧转发）
  → 127.0.0.1:11800    webdock-failover-proxy（主备切换）
      ├─ 主 127.0.0.1:11810 ← webdock2 反向隧道（2026-07-02 起）
      └─ 备 127.0.0.1:11811 ← webdock1 反向隧道
```

- **主备关系的唯一权威来源是 aliecs 上 `/etc/default/webdock-failover-proxy` 的 `WEBDOCK_FAILOVER_PRIMARY_NAME` / `WEBDOCK_FAILOVER_STANDBY_NAME`**（当前 primary=webdock2，standby=webdock1；2026-07-04 实测 `127.0.0.1:11800/healthz` 返回 `X-Webdock-Route: primary`、`X-Webdock-Device: webdock2`）。⚠️ 端口只是当前实现细节：当前 primary 端口为 11810、standby 端口为 11811，但切换时可能改端口或改名称绑定，勿只凭端口号或代码默认值判断主备。
- 主上游失败自动切备（标记 60s），并给回复加"已自动切换备用服务器"前缀。
- 代理在响应头标注实际来源：`X-Webdock-Device`（设备名，来自环境文件 `WEBDOCK_FAILOVER_PRIMARY_NAME`/`STANDBY_NAME`）和 `X-Webdock-Route`（primary/standby）；bridge 用它渲染飞书卡片灰色脚注（例如 `设备: webdock2(主) | 项目: xx | 耗时: Ns`，以实际响应头为准）。
- 切换主备 = 改该环境文件里的 `PRIMARY_*` / `STANDBY_*`（尤其是 `*_NAME` 与对应 host/port 绑定）后 `systemctl restart webdock-failover-proxy`，不改代码。
- 判定某条消息由哪台处理：查各机 `/var/log/webdock/archive/<UTC日期>.jsonl`（权威记录）。

### 腾讯云 P0 并行路径（尚未切生产）

```
webdock2 WSL
  ├─ 原生产业务隧道 → aliecs 127.0.0.1:11810
  ├─ console 隧道   → aliecs 127.0.0.1:16090/16091
  └─ 新业务隧道     → txecs 127.0.0.1:11810

txecs 127.0.0.1:11800 failover-proxy
  ├─ 主 127.0.0.1:11810 ← webdock2（已验证）
  └─ 备 127.0.0.1:11811 ← webdock1（设备离线，待验证）
```

- 当前生产 bridge 仍指 AliECS 11800；txecs 11800 只做 P0 回环健康验证。
- 两个落点的 11810 是不同主机上的回环端口，不冲突。
- txecs 主备权威由 txecs `/etc/default/webdock-failover-proxy` 和
  `11800/healthz` 响应头共同确认；不得拿 AliECS 的环境文件判断 txecs。
- P1 只切 bridge 目标，不迁移 WebDock browser_data，也不改变 console
  隧道落点。

## 设备档案

### aliecs（生产服务器）

- 别名：服务器。
- 入口：`ssh aliecs`（root@47.77.176.62）。ECS **不在 tailnet**。
- 运行容器：backend-api / public-web / admin-ui / doc-sync-worker / tplus-sync-worker / mcp-coding-server（同一 V-tag，来自 AliECS release）、postgres:16、`openclaw-bridge`（独立 V-tag，手动 cutover）、openclaw 网关（上游官方镜像，配置在主机 `/root/.openclaw`，不进 git，restic 备份）、sing-box。
- 主机层：nginx（配置入 infra 仓库；**MCP OAuth 的 `/.well-known/*` 路由是手工加的，重建会丢**）、三个隧道代理 systemd 服务（webdock-failover-proxy、webdock-tunnel-proxy、immich-tunnel-proxy，脚本在 `/opt/aliecs/`）。
- 隧道端口（127.0.0.1）：11800 为 webdock failover 入口；当前 11810←webdock2 主、11811←webdock1 备（以 `/etc/default/webdock-failover-proxy` 的 NAME 绑定为准）；12283←webdock1 Immich、18015/18016←webdock1 AdventureLog，另有 Gokapi/Authentik 隧道（端口见 webdock1 各 unit 的 env）。
- 远程控制台（2026-07-04，`https://hydwang.xyz/console/`）：nginx `/console/*` 七路 location（认证=Authelia `two_factor` + lldap `console_admins` 组，成对 deny 兜底；**VNC 层免密设计**，2FA 是唯一闸门）；本机组件 ttyd 7681（unit `ttyd-console`，⚠️ apt 自带 `ttyd.service` 抢端口须 disable）、webtop 3000 按需启停（`/opt/aliecs/aliecs-temp-desktop.sh`，2G 内存用完必须 stop）；ECS `authorized_keys` permitlisten 新增四条 160xx（16080/16081←webdock1、16090/16091←webdock2，与生产 118xx 隔离）。详见 infra `console/README.md`。
- 部署：push AliECS main → release-deploy 自动构建部署业务镜像；bridge 镜像同流程构建，但运行切换需在 Actions 手工触发一次 `bridge-cutover`（无需填 tag，自动解析 digest、验证并失败回滚）。
- 排障：`docker ps`、bridge 日志 `docker logs openclaw-bridge`、部署尖峰时 health 告警多为瞬时（2G 内存超卖）。

### txecs（腾讯云 business-cn 候选主栈）

- 入口：`ssh txecs`（`ubuntu@106.52.51.67`）；系统 Ubuntu 24.04，
  4 vCPU、约 3.6 GiB 可用内存。
- 当前是 P0 隔离栈，不是生产写端；备案通过和 Stage I 授权前，生产域名、
  webhook、worker、OpenClaw、bridge 均不得切入。
- 主机重建入口：infra
  `roles/server/{common,tencent}`；age 私钥和 GitHub infra 只读 deploy
  key 是最小人工输入。
- 应用部署：AliECS `release-deploy` 的独立 `business-cn` job；
  `/srv/business-cn/current` 记录当前源码提交，镜像从 TCR 按 digest 拉取。
- WebDock P0：`127.0.0.1:11800` 为 failover 入口，
  `11810←webdock2` 已验证，`11811←webdock1` 待设备上线。
- 公网边界：UFW P0 不开放 80/443，nginx 默认站点返回 444；无 sing-box、
  mihomo 或任何第三方出海转发能力。
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
- 反向隧道：WSL 内两条独立业务隧道均将 WebDock 18000 映射到远端
  11810：原 `webdock-ecs-tunnel` 落 AliECS（当前生产），新
  `webdock-business-tunnel` 落 txecs（P0 并行）；console 仍单独落 AliECS。
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
| `huozao/AliECS` | aliecs + txecs | push main → 构建并同步 GHCR/TCR；aliecs `edge-us` 与 txecs `business-cn` 为独立 deploy job；**bridge 运行切换仍需手动 cutover 且 P1 才允许改目标**；改动走 PR |
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
- bridge 换镜像：Actions 手动触发一次 `bridge-cutover` workflow（无需填 tag，失败自动回滚），运行状态在 `/root/infra/server/release.env`；禁止登机手改版本。

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
