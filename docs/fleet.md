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

txecs 上另有三条同形的容器侧通路。四条规则都只允许
`business-cn` 容器子网访问 Docker 网关，不开放到公网：

| 容器访问 | 宿主机转发目标 | 用途 |
|---|---|---|
| `host.docker.internal:11800` | `127.0.0.1:11800` | WebDock 照片存储 |
| `host.docker.internal:18080` | `127.0.0.1:18080` | 企微客服处理器 |
| `host.docker.internal:18200` | `127.0.0.1:18200` | ERPNext 资料任务归档 |
| `host.docker.internal:18201` | `127.0.0.1:18201` | Paperless 资料任务归档 |

2026-08-21 实测迁移后四条都曾出现“宿主机回环可达、容器超时”；根因是回环服务没有
对应的 Docker 网关 proxy/UFW 放行。验活必须从 `business-cn-backend-api-1` 容器内发起，
只在宿主机 `curl 127.0.0.1:<port>` 不能证明业务通路恢复。

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
  └─ 备 127.0.0.1:11811 ← webdock1（已验证）
```

- 当前生产 bridge 已迁 txecs，并经 txecs `127.0.0.1:11800` 访问
  webdock2 主力；AliECS 原 bridge/gateway 保持停止。
- 两个落点的 11810 是不同主机上的回环端口，不冲突。
- txecs 主备权威由 txecs `/etc/default/webdock-failover-proxy` 和
  `11800/healthz` 响应头共同确认；不得拿 AliECS 的环境文件判断 txecs。
- WebDock browser_data 不随业务服务器迁移；console 隧道仍落 AliECS。

## 设备间既存通道（要传文件/镜像前先看这里，别重新摸索）

**已存在、可复用**：

| 方向 | 形态 | 凭据/配置 | 本职用途 | 能否复用 |
|---|---|---|---|---|
| aliecs ↔ txecs | `peer-channel` 受限本地转发 + Syncthing TLS；发布 aliecs→txecs、ACK 反向 | 复用 txecs 的 `peer-backup.key` 私钥，但使用独立 `peer-channel` 公钥身份；只准转发 aliecs loopback 18080/22000 | 主镜像发布、ACK、txecs→aliecs append-only Restic | **主路径**。无 shell/SFTP/任意端口转发；2026-08-09 已通过断线续传、哈希、备份、恢复和本地维护验收 |
| txecs → aliecs | 旧 `restic-peer` SFTP，chroot 到 `/srv/peer-restic` | 同一把 txecs 私钥的独立 `restic-peer` 公钥身份 | 历史每日备份与手工镜像旁路 | **仅回退**。不再承载日常备份；身份和配置暂保留，删除须另行确认 |
| webdock1 ↔ webdock2 | Restic over SFTP(:2222) + SMB | `webdock-peer-backup.enc.env` | 两机互为加密备份 | 未验证他用 |
| webdock2 → txecs | SSH 反向隧道 11810 | infra roles | 生产业务隧道（bridge 主路） | 勿占用 |
| webdock2 → aliecs | SSH 隧道 16090/16091 | infra roles | console 通道 | 勿占用 |

`peer-channel` 是一条物理传输通道，不等于一个部署单元。当前在同一目录协议上承载两类
互不连坐的 release：

| release type | 镜像 | txecs 部署器 | 状态/回滚/ACK |
|---|---|---|---|
| `business-cn` | 6 个：public-web、admin-ui、backend-api、doc-sync-worker、tplus-sync-worker、mcp-coding-server | `deploy-role.sh business-cn` | `/srv/business-cn/current`；业务栈整体回滚；独立 ACK |
| `openclaw-bridge` | 1 个：openclaw-bridge | `txecs-openclaw-bridge.service` | `/srv/internal-stack/release.env`；只回滚 bridge；独立 ACK |

因此“6+1”只是服务生命周期边界，不是两条网络线路。两类 release 都由 aliecs 从 GHCR
按 digest 导出，经同一个 Syncthing 通道送到 txecs，并在 txecs 自动部署；任一类失败不会
触发另一类重启或回滚。TCR 镜像继续异步保留，`bridge-cutover.yml` 只作手工备用。

2026-08-09 对 peer-channel 做过 96 MiB 随机文件实测：传输中断开隧道 8 秒后自动
续传，两端 SHA-256 一致；含故障窗口约 56 秒，等效 1.71 MiB/s。Syncthing 限制为
`MemoryHigh=256M`、`MemoryMax=384M`，rest-server 为 `MemoryMax=128M`；这是上限，
不是常驻占用承诺。

**确认不存在或不应假设存在的连接**：

- aliecs ↔ txecs **没有管理员 shell 互信**。仅有 txecs 主动连接 aliecs 的
  `peer-channel` 受限身份；它不能执行命令、SFTP、agent/X11/tunnel，也不能转发
  声明外端口，不得当作通用 SSH 凭据。
- txecs **没装 tailscale**，连不到 webdock1/webdock2 的 100.x 地址，因此
  **GitHub Actions 无法经 txecs 跳板触达 webdock**；webdock 侧只能主动外连。
- txecs **没有持久的 TCR 凭据**，部署时由 CI 通过环境变量传入；`runtime.env` 里只有
  `IMAGE_REGISTRY_BASE`。TCR **push** 凭据只在 GitHub Actions secrets，不在 SOPS。

**镜像拉取能力**（同上日实测）：

| 主体 | 拉 GHCR | 备注 |
|---|---|---|
| aliecs | **12.97 MB/s** | 早已长期 `docker login ghcr.io`，`/root/.docker/config.json` 里有 |
| webdock2 | 3.7 MB/s | 经 mihomo；出口是 aliecs。mihomo 已按国内/国外分流，**访问国内目标本来就直连**（实测出口=家宽 IP），不需要改 `NO_PROXY`，更不该为此重启 docker（会打断 ChatGPT 会话，触碰红线） |
| txecs | 13.6 KB/s | 实质不可用，别把它当备选 |
| GitHub runner → TCR | 劣化时 **0 B/s 冻结** | 判别与处置见 `runbooks/deploy.md` |

## SSH 账户与认证姿态（改 sshd 前必读，改错直接断生产）

> **本节两类内容，过期方式不同**：账户清单、红线、排序陷阱属**持久约束**，
> 变化时应随改动更新本节；带具体数字的是 **2026-08-08 的取证快照**，只用来支撑
> 当时的判断，不代表当前状态。要重新评估（比如再次考虑收紧 SSH）时**必须重新取证**，
> 不要直接引用下面的数字：
>
> ```bash
> # 各机真实登录账户与所用 key（换成目标设备）
> ssh txecs 'sudo journalctl -u ssh --since "30 days ago" | \
>   grep -oE "Accepted publickey for [a-z]+ from [0-9.]+" | awk "{print \$4}" | sort | uniq -c'
> ssh txecs 'sudo sshd -T | grep -E "^(passwordauthentication|permitrootlogin|maxauthtries) "'
> ssh txecs 'sudo fail2ban-client status sshd'
> ```

**各机实际登录账户**（2026-08-08 实测，别假设"只有一个管理员账户"）：

| 设备 | 账户 | 用途 | 形态 |
|---|---|---|---|
| txecs | `ubuntu` | devbox 人工管理 + GitHub Actions 部署 | 唯一日常 shell 账户 |
| txecs | `webdock-tunnel` | 生产隧道 11810/11811、console 16080/16081/16090/16091/16101、ERP 18200/18201 | `Match User` 块，`PermitListen` 锁定这九个端口 |
| txecs | `artifact-drop` | artifact 投递 | SFTP chroot，拿不到 shell |
| txecs | `restic-peer` | 跨机备份接收 | SFTP chroot，拿不到 shell |
| aliecs | `root` | 人工/CI 管理、bridge/console/T+ 隧道、ProductCenter | 5733 次登录（2026-07-09~08-08 区间计数），历史取证时 `authorized_keys` 8 个 key |
| aliecs | `peer-channel` | txecs→aliecs 备份和双向发布/ACK 传输 | 无 shell；仅 local forward 到 127.0.0.1:18080/22000 |

**两条已踩过的红线**（2026-08-08 各拦下一次）：

- txecs **不能**设 `AllowUsers ubuntu` —— 会同时断掉 `webdock-tunnel`（生产 bridge 主路）、
  `artifact-drop`、`restic-peer`。要写就必须四个账户全列，但那是个会随新账户增长的动态清单，
  漏一个就静默断链，收益却接近零（挡掉的只有 root 和无 key 的系统账户）。
- aliecs **不能**设 `PermitRootLogin no` —— 那台机器所有自动化都走 root key，设了等于全断。
  它的正确值是 `prohibit-password`（在 `00-enable-login.conf`）。

**因此 infra 的共享片段 `server/ssh/05-hardening.conf` 刻意只管
`PasswordAuthentication` / `KbdInteractiveAuthentication`**；`PermitRootLogin` 与
`MaxAuthTries` 由各 role 自己设（txecs 在 `40-business-cn.conf` 里 `MaxAuthTries 3`，
因为各客户端都是 IdentitiesOnly 单 key；aliecs 保持默认 6，因为 root 下挂了 8 个 key）。

**排序陷阱（sshd 是 first-match-wins）**：最先出现的关键字生效，`sshd_config.d/` 按文件名
排序加载。txecs 的 `50-cloud-init.conf` 里带着 `PasswordAuthentication yes`，全靠
`05-hardening.conf` 排在它前面压制——**新增 drop-in 时编号必须小于 40**，否则会掉进
`40-business-cn.conf` 的 `Match` 块作用域，只对单个用户生效。改完必须用
`sshd -T` 验生效值、`sshd -T -C user=webdock-tunnel` 验 Match 块没被污染。

**IP 白名单不可行**——结论持久，依据是 2026-08-08 的快照（`2026-08-01~08-08` 区间计数）：
devbox 家宽 IP 在这 7 天里就变过（`222.210.79.47` / `171.221.110.108`，同一把 key），
GitHub Actions 走 Azure 动态段（同期 3 个不同 IP）。收白名单会同时锁死自己和断掉 CI 部署。
**结论不依赖具体数字**：只要家宽是动态 IP、CI 跑在公有云 runner 上，这条就成立；
哪天改成固定 IP 或自建 runner，可以重新评估。
当时的防护基线：key-only（`PasswordAuthentication no`）+ fail2ban（bantime 1 天，
累计封 105 个），爆破 348 次/7 天属互联网背景水平、不是定向攻击。

## 设备档案

### aliecs（海外边界与 business-cn 隔离候选）

- 别名：服务器。
- 入口：`ssh aliecs`（root@47.77.176.62）。ECS **不在 tailnet**。
- 当前运行容器包括旧 public-web/admin-ui/postgres（只作回滚）和
  mcp-coding-server；旧 SSO、业务写端、worker、OpenClaw 和 bridge 均停止。
- 公网 Nginx 只启用 console、ERP、Immich 和默认拒绝；其他历史 server 块隔离在
  `/etc/nginx/conf.inactive.d/`。console 的 forward-auth 在 txecs 完成，
  AliECS 源站只允许 txecs 访问。Immich 经 webdock1 的 12283 隧道提供公网入口；
  `https://immich.hydwang.xyz/api/server/ping` 已于 2026-08-01 外部验证为 200。
- 反向迁移候选使用独立 `/srv/business-cn` 和 `business-cn-*` 容器。
  在线准备阶段只允许空 PostgreSQL，不启动业务/worker，不覆盖 console/ERP。
- 隧道端口（127.0.0.1）：11800 为 webdock failover 入口；当前 11810←webdock2 主、11811←webdock1 备（以 `/etc/default/webdock-failover-proxy` 的 NAME 绑定为准）；12283←webdock1 Immich、18015/18016←webdock1 AdventureLog、15342←webdock1 Gokapi、16080/16081←webdock1 console noVNC（2026-08-04 实测；unit 名见 webdock1 档案）。2026-08-01 实测 webdock1 无 Authentik 容器或 tunnel unit，不再把它列作当前运行事实。

  所有承载 `ssh -R` 的服务端（aliecs、txecs）必须有 `ClientAliveInterval` 非 0，否则对端失联时
  转发端口永不释放、隧道无限重连。配置由 `infra` 的 `server/ssh/10-tunnel-keepalive.conf`
  统一下发，`roles/server/{tencent,aliecs-edge}/verify.sh` 各有断言。查：`sshd -T | grep clientalive`。
- 远程控制台（2026-07-04，`https://hydwang.xyz/console/`）：nginx `/console/*` 七路 location（认证=Authelia `two_factor` + lldap `console_admins` 组，成对 deny 兜底；**VNC 层免密设计**，2FA 是唯一闸门）；本机组件 ttyd 7681（unit `ttyd-console`，⚠️ apt 自带 `ttyd.service` 抢端口须 disable）、webtop 3000 按需启停（`/opt/aliecs/aliecs-temp-desktop.sh`，2G 内存用完必须 stop）；ECS `authorized_keys` permitlisten 新增四条 160xx（16080/16081←webdock1、16090/16091←webdock2，与生产 118xx 隔离）。⚠️ 2026-08-03 起 `/console/devbox/desktop/` 已改为在 txecs 本地终止，不再回源本机；AliECS 侧对应的 16101 location 与 authorized_keys 条目**刻意保留**，仅作回滚路径，不再有流量。详见 infra `console/README.md`。
- 部署：push AliECS main → release-deploy 构建镜像；获授权后业务 6 镜像选择 `business-cn`。当 `deploy/openclaw-bridge/**` 的内容树 hash 变化时，bridge 自动走同一 peer-channel，但使用独立 release、systemd 切换、失败回滚和 ACK；bridge 没变的合并不碰它。手工重发选 `bridge-peer`；TCR 回滚才用 `bridge-cutover` 的 `workflow_dispatch`（填 `CUTOVER_TXECS`）。
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
- 应用部署：AliECS `release-deploy` 的 `business-cn` job；aliecs 从 GHCR 按 digest
  导出后经 peer-channel 传入，txecs 校验、`docker load`、自动部署并回 ACK。
  `/srv/business-cn/current` 记录当前源码提交；TCR 只作显式 fallback。
- bridge 部署：同一个 peer-channel 的 `openclaw-bridge` release；txecs 校验、离线加载后
  只更新 `/srv/internal-stack/release.env` 并重启 `txecs-openclaw-bridge.service`，健康检查
  或运行 digest 不符会自动恢复旧 state。它不调用 business-cn 部署器，也不重启 6 个业务镜像。
- WebDock：`127.0.0.1:11800` 为 failover 入口，
  `11810←webdock2`、`11811←webdock1` 均已验证；当前响应头仍为
  `X-Webdock-Device: webdock2`、`X-Webdock-Route: primary`。
- 公网边界：UFW 开放 80/443；Nginx 默认站点仍返回 444。`@`/`www`、
  `auth`/`lldap` 均在本机终止 TLS，Authelia/LLDAP 也在本机运行。无
  sing-box、mihomo 或任何第三方出海转发能力。
- **DNS 直连、不经 Cloudflare 代理（2026-08-08 起）**：`@`/`www`/`auth`/`lldap`
  四条记录已转灰云 DNS-only，公网直接解析到 `106.52.51.67`。
  **排障时看到客户端直连源站 IP 是正常的，不是配置泄漏**；nginx 日志里的
  `remote_addr` 现在就是真实客户端 IP（`cloudflare-realip.conf` 在灰云下不生效）。
  改回橙云的完整步骤与代价见 infra `docs/runbooks/site-entry.md`「代理模式」。
  ⚠️「只有 `erp` 仍指 aliecs `47.77.176.62`」这句自 2026-08-16 起失效：aliecs 因当月
  公网流量超支停机，`erp` 已改指 txecs `106.52.51.67`（`services.json` 的
  `public_edge` 由 `aliecs-edge` 改成 `business`，DNS 由 opentofu 渲染）。
  ⚠️「**现在没有任何生产域名指向 aliecs**」这句自 2026-08-20 起确认会误导：它只盘点了
  business 业务域名，漏掉了 `immich`/`adventure`/`adventure-media`/`files` 四条——这四条
  当时仍解析到 `47.77.176.62`，`services.json` 里标着 `status: suspended`（所以 opentofu
  的 `active_records` 根本不管它们，Cloudflare 上是手工遗留记录），实际却一直在对外服务，
  aliecs 停机后全部 502。四条已于 2026-08-20 迁到 txecs，新情况见本文件 txecs 段
  「webdock1 服务入口」与 infra `roles/webdock/extras/README.md`。
  **教训：`suspended` 只说明 tofu 不管它，不说明它没在跑。**
- 远程控制台：公网 `/console/*` 的 Authelia forward-auth 在本机完成。
  ⚠️「除 `/console/devbox/desktop/` 外均反代回 AliECS 源站」这句自 2026-08-16 起失效：
  同日 webdock1/webdock2 的 browser+desktop 四条路径也改成本机终止，通用
  `location /console` 虽然仍写着回源 aliecs，但已经没有路径会落到它。
  本机终止的五条及其 loopback 上游：`devbox/desktop`←16101（2026-08-03）、
  `webdock1/browser`←16080、`webdock1/desktop`←16081、`webdock2/browser`←16090、
  `webdock2/desktop`←16091（均 2026-08-16）。sshd `PermitListen` 与
  `webdock-tunnel` authorized_keys 均由 infra `roles/server/tencent` 管理；
  设备侧 unit 是 `console-txecs-tunnel.service`（旧的 `console-ecs-tunnel` 已 disable，
  两者不能同时跑，上游端口号相同）。
- **webdock1 服务入口（2026-08-20 从 aliecs 迁入）**：`immich.hydwang.xyz`←12283、
  `adventure.hydwang.xyz`←18015、`adventure-media.hydwang.xyz`←18016、
  `files.hydwang.xyz`←15342（Gokapi）。四个 loopback 上游都由 webdock1 的反向隧道维持，
  隧道断则 502；nginx 是 `conf.d/7{0,1,2}-*.conf`，证书 DNS-01 单独签、与主站分开。
  设备侧 unit：`webdock-immich-tunnel` / `adventurelog-tunnel` / `gokapi-ecs-tunnel`，
  各用独立 key（见 infra `roles/webdock/extras/README.md`）。
  ⚠️ 端口双层限制：key 的 `permitlisten=` 和 `Match User webdock-tunnel` 的
  `PermitListen` 都要放行，只改一层报 `remote port forwarding failed`。
  ⚠️ `adventure-media` 的根路径不应答（backend 固有行为），探活用 `/api/`，别据此判故障。
- 排障：
  `sudo systemctl status webdock-failover-proxy`、
  `curl -i http://127.0.0.1:11800/healthz`、
  `readlink -f /srv/business-cn/current`、
  `ss -ltn | grep 16101`（devbox console 隧道）。

### webdock1（旧笔记本，当前备用）

- 别名：旧电脑；ssh alias `webdock` / `webdock1` / `WebDock01`；tailscale/hostname `webdock-laptop`；用户 `webdock`。
- 运行：`webdock` 容器（ghcr.io/huozao/webdock:sha-xxx）+ Chrome/ChatGPT 登录态（browser_data 卷，**登录必须人工做，红线**）、noVNC `http://100.97.176.57:6080/`、Immich(2283)、AdventureLog(8015/8016)、Gokapi；2026-08-01 实测无 Authentik 运行实例。
- 反向隧道 unit（systemd，2026-08-04 实测五条）：

  | unit | 落点 | 端口 |
  |---|---|---|
  | `webdock-business-tunnel` | txecs | `-R 11811` webdock API（备）+ `-L 18020` 回连 backend |
  | `webdock-immich-tunnel` | aliecs | `-R 12283` Immich |
  | `adventurelog-tunnel` | aliecs | `-R 18015/18016` |
  | `console-ecs-tunnel` | aliecs | `-R 16080/16081` noVNC |
  | `gokapi-ecs-tunnel` | aliecs | `-R 15342` Gokapi |

  ⚠️ unit 命名不统一，只有两条以 `webdock-` 开头。`systemctl list-units 'webdock*tunnel*'`
  会漏掉后三条并给出"只有两条隧道"的错误结论。要列全用
  `systemctl list-units --type=service | grep -iE 'tunnel'` 或 `ps -eo pid,cmd | grep 'ssh .*-R '`。
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

### WebDock 双向加密备份

- webdock1 每日把 Immich、Gokapi、AdventureLog、WebDock 登录态与日志写入
  webdock2 的 `C:\WebDockPeerBackup`；PostgreSQL 先做逻辑导出，不复制运行中的 data dir。
- webdock2 每日把 WebDock 登录态与日志写入 webdock1 的
  `/srv/webdock-peer-backup/webdock2/repo`。接收端是只绑定 Tailscale IP:2222、
  强制 internal-sftp 且无 shell 的独立 sshd。
- 两端传输经 Tailscale；Windows 方向另强制 SMB 3.1.1 `seal`。两份仓库均由
  Restic 加密，共享密码只存在 infra 的 SOPS 密文中，两台设备都能解密并打开。
- 资产契约、保留期与恢复探针的权威源是 infra
  `config/backup/webdock-peer-assets.json`；以实际 timer、最近 snapshot 和
  `webdock-peer-restore-check.service` 结果判定是否健康，不从本段推断。

### devbox（开发机）

- 别名：开发机、本机。Windows 11，工作区 `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock`。
- 持有：AliECS、webdock、infra 三个仓库克隆；`gh` 已认证（nihil7）；`~/.ssh/config` 定义全部设备别名。
- 不运行生产代码。
- 远程控制台（`https://hydwang.xyz/console/devbox/desktop/`）：TightVNC :5900 免密
  （防火墙 `DevboxConsole-VNC-5900` 只放行 `100.64.0.0/10`）+ websockify/noVNC 6101 +
  `console-tunnel`（`-R 16101`）。**2026-08-03 起隧道落 txecs，不再落 aliecs**：
  原链路每帧往返两次太平洋，实测 1.2–2.3s，改后 0.34s。2FA 闸门不变。
- 装机/重建与对账剧本：infra `roles/devbox/README.md`；设备参数
  `infra/config/devices/devbox.env`（换 console 边界只改这里两行）。
- 低延迟捷径：tailnet 内可原生 VNC 直连 `100.116.248.82:5900`，不经公网与域名。
- 验证：`pwsh -File infra/roles/devbox/windows-native/apply.ps1 -CheckOnly`。

## 仓库 ↔ 设备映射

| GitHub 仓库 | 部署到 | 部署链路 |
|---|---|---|
| `huozao/AliECS` | aliecs + txecs | push main → GHCR 源制品 + SBOM；aliecs 按 digest 预拉并经 peer-channel 分发，txecs 自动校验/部署/ACK；TCR 异步镜像只作 fallback；改动走 PR |
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
- bridge 换镜像：改 `deploy/openclaw-bridge/**` 并合入 main 后自动生成独立 peer release，txecs 自动校验/切换/回滚/ACK；手工重发选 `release-deploy` 的 `bridge-peer`。`bridge-cutover` 仅为 TCR 手工备用。运行状态在 `/srv/internal-stack/release.env`；禁止登机手改版本。

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
