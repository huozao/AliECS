# 设备清单（Fleet）

> 单一事实源：设备 ↔ 职责 ↔ 仓库 ↔ 部署链路的权威映射。
> 所有事实于 2026-07-02 经 SSH 实测核实。发现与本文不符时，以实测为准并更新本文。

## 命名规则

- 逻辑名 = 角色 + 序号，小写字母数字（如 `webdock1`、`webdock2`），**不用硬件序列号**。
- 逻辑名跟角色走：换硬件接替同一职责时逻辑名不变，只更新档案里的硬件字段。
- 同一个逻辑名用作 ssh alias、tailscale 机器名，保持一致；历史叫法登记进"别名"字段。
- 业务代码不硬编码设备名（已核实：两仓库代码零命中）。将来若代码需区分实例，用环境变量（如 `DEVICE_NAME=webdock2`）注入。

## 设备总表

| 逻辑名 | 角色 | 硬件/OS | 网络入口 | 运行代码来源 |
|---|---|---|---|---|
| `devbox` | 开发机 | 本地 Windows 11 | 本机 | 3 个仓库克隆（不运行生产） |
| `aliecs` | 生产服务器 | 阿里云 ECS us-west-1 / Ubuntu，2G 内存 | `ssh aliecs`（root@47.77.176.62） | AliECS 全部镜像 + bridge 镜像 + infra 主机配置 |
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

- **主备关系的唯一权威来源是 aliecs 上 `/etc/default/webdock-failover-proxy`**（当前 PRIMARY_PORT=11810 即 webdock2 为主，2026-07-02 切换）。⚠️ 代理脚本 `/opt/aliecs/webdock-failover-proxy.py` 的内置默认值与实际配置**正好相反**，勿以代码默认值判断主备。
- 主上游失败自动切备（标记 60s），并给回复加"已自动切换备用服务器"前缀。
- 代理在响应头标注实际来源：`X-Webdock-Device`（设备名，来自环境文件 `WEBDOCK_FAILOVER_PRIMARY_NAME`/`STANDBY_NAME`）和 `X-Webdock-Route`（primary/standby）；bridge 用它渲染飞书卡片灰色脚注（`设备: webdock1(主) | 项目: xx | 耗时: Ns`）。
- 切换主备 = 改该环境文件后 `systemctl restart webdock-failover-proxy`，不改代码。
- 判定某条消息由哪台处理：查各机 `/var/log/webdock/archive/<UTC日期>.jsonl`（权威记录）。

## 设备档案

### aliecs（生产服务器）

- 别名：服务器。
- 入口：`ssh aliecs`（root@47.77.176.62）。ECS **不在 tailnet**。
- 运行容器：backend-api / public-web / admin-ui / doc-sync-worker / tplus-sync-worker / mcp-coding-server（同一 V-tag，来自 AliECS release）、postgres:16、`openclaw-bridge`（独立 V-tag，手动 cutover）、openclaw 网关（上游官方镜像，配置在主机 `/root/.openclaw`，不进 git，restic 备份）、sing-box。
- 主机层：nginx（配置入 infra 仓库；**MCP OAuth 的 `/.well-known/*` 路由是手工加的，重建会丢**）、三个隧道代理 systemd 服务（webdock-failover-proxy、webdock-tunnel-proxy、immich-tunnel-proxy，脚本在 `/opt/aliecs/`）。
- 隧道端口（127.0.0.1）：11811←webdock1 主、11810←webdock2 备、12283←webdock1 Immich、18015/18016←webdock1 AdventureLog，另有 Gokapi/Authentik 隧道（端口见 webdock1 各 unit 的 env）。
- 部署：push AliECS main → release-deploy 自动构建部署业务镜像；bridge 镜像同流程构建但 **cutover 永远手动**（改 `/root/infra/server/.env` 的 `OPENCLAW_BRIDGE_TAG`，先 `docker rm -f openclaw-bridge` 再 compose up）。
- 排障：`docker ps`、bridge 日志 `docker logs openclaw-bridge`、部署尖峰时 health 告警多为瞬时（2G 内存超卖）。

### webdock1（旧笔记本，当前备用）

- 别名：旧电脑；ssh alias `webdock` / `webdock1` / `WebDock01`；tailscale/hostname `webdock-laptop`；用户 `webdock`。
- 运行：`webdock` 容器（ghcr.io/huozao/webdock:sha-xxx）+ Chrome/ChatGPT 登录态（browser_data 卷，**登录必须人工做，红线**）、noVNC `http://100.97.176.57:6080/`、Immich(2283)、AdventureLog(8015/8016)、Gokapi、Authentik。
- 反向隧道 unit（systemd）：`-R 11811`（webdock API，备）、`-R 12283`（Immich）、`-R 18015/18016`（AdventureLog）、Gokapi/Authentik（env 参数化）。
- 部署：拉新镜像 + `systemctl restart webdock`（卷不丢登录态）。webdock 仓库小改可直推 main（**直推前本地 pytest**，CI 不跑直推）。
- 日志：消息存档 `/var/log/webdock/archive/<UTC日期>.jsonl`（收发全文+lane+status）；容器内 `/app/logs/`。
- 验证：`ssh webdock1 'curl -fsS http://100.97.176.57:18000/healthz'`。
- 硬件注意：合盖不挂起已配好（可当服务器）；断电史见运维记忆，建议 BIOS 来电自启。

### webdock2（新台式机，当前主力）

- 别名：新电脑、desktop；ssh alias `desktop` / `webdock2` / `WebDock02`；Windows 主机名 `DESKTOP-D0LV1TN`；用户 `Admin`。
- 结构：**SSH 登录进的是 Windows（PowerShell）**，WebDock 跑在 WSL2 发行版 `Ubuntu-24.04-WebDock` 内的 docker 里。Linux 命令一律 `ssh webdock2 "wsl -d Ubuntu-24.04-WebDock -- <cmd>"`。
- 运行：仅 `webdock` 容器（与 webdock1 同镜像同 tag）。Immich / AdventureLog / Gokapi 暂不部署（按需再拉）。
- 反向隧道：WSL 内 `-R 127.0.0.1:11810:127.0.0.1:18000`（主）。
- noVNC：`http://100.67.38.52:6080/` 可用。⚠️ 已知怪癖：Tailscale 直连 `100.67.38.52:18000` 返回 502（Windows→WSL 端口转发问题），**生产链路不受影响**（隧道从 WSL 内 localhost 拉出）；从 ECS 探 `127.0.0.1:11810/healthz` 才是有效健康检查。
- 日志：WSL 内 `/var/log/webdock/archive/`。

### devbox（开发机）

- 别名：开发机、本机。Windows 11，工作区 `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock`。
- 持有：AliECS、webdock、infra 三个仓库克隆；`gh` 已认证（nihil7）；`~/.ssh/config` 定义全部设备别名。
- 不运行生产代码。

## 仓库 ↔ 设备映射

| GitHub 仓库 | 部署到 | 部署链路 |
|---|---|---|
| `huozao/AliECS` | aliecs | push main → release-deploy 自动构建部署；**bridge 镜像源码在 `deploy/openclaw-bridge/`，构建后手动 cutover**；改动走 PR |
| `huozao/webdock` | webdock1 + webdock2 | CI 构建 sha-tag 镜像 → 各机手动拉取重启；两机应保持同 tag；小改可直推 main |
| `huozao/infra`（私有） | aliecs 主机层 | 手动同步，无 CI；nginx、bridge compose、chain-logger |

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
- bridge 换镜像：Actions 手动触发 `bridge-cutover` workflow（填 tag，失败自动回滚），替代登机手工 runbook。

## 新增设备流程

1. 按"角色+序号"起逻辑名（如 `webdock3`）。
2. tailscale 机器名、ssh alias 与逻辑名一致（写入 devbox `~/.ssh/config`）。
3. 在本文档"设备档案"复制一节模板，填全 7 项：别名 / 硬件与OS / 运行什么 / 隧道与端口 / 部署方式 / 日志位置 / 验证命令。
4. 更新"设备总表"和工作区根 `AGENTS.md` 的简表。
5. 若接入 webdock 主备池：在新机建反向隧道 unit（分配新 118xx 端口），并按需更新 `/etc/default/webdock-failover-proxy`（含 `*_NAME` 设备名两行）。
6. ⚠️ webdock 节点必须初始化 `browser_data/runtime.json`（从现有节点复制：`media_base_url` + 三个超时参数）。缺 `media_base_url` 时飞书出图/表格截图整条链路静默失效（2026-07-02 webdock2 踩坑）。

## 已知遗留问题（记录未处理）

- webdock2 Tailscale 直连 18000 返回 502（仅影响外部直连调试）。
- MEDIA 图片 token 存储在生成它的那台 webdock 本机：主备切换后，切换前发出的旧图片链接会 404（图片链接本就短期使用，暂接受）。
