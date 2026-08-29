# Runbook：部署 / CI / 回滚 / 502 排障

## 链路图

```
push main（PR 合并）
  → GitHub Actions build-push：镜像标签 = 目录 git tree hash（t-xxxxxxxxxxxx），
    manifest 命中即跳过构建；sha-<commit> 只是同批次 GitHub 别名
  → 获授权后 workflow_dispatch 选择 business-cn
  → aliecs 按 digest 从 GHCR 导出镜像和 SPDX SBOM
  → Syncthing 经受限 SSH loopback 通道传到 txecs
  → txecs 校验 release.json/SHA-256，离线缓存部署并回传 ACK
  → /srv/business-cn/current：按目录导出 *_TAG → 迁移（db/ 未变更即跳过）
  → docker compose up -d（内容没变的服务不重建容器）
  → 三个生产 worker 运行守卫 → healthcheck
  → 写 last-success-commit + 部署清单（commit/run/attempt/各镜像 digest）

bridge 构建上下文变化（或手工选择 bridge-peer）
  → aliecs 按 digest 从 GHCR 导出 openclaw-bridge + SPDX SBOM
  → 同一个 Syncthing peer-channel 传到 txecs
  → txecs 校验并 docker load
  → 独立更新 /srv/internal-stack/release.env
  → 重启 txecs-openclaw-bridge.service → /v1/models + 运行 digest 验证
  → 失败只回滚 bridge；成功回传独立 ACK
```

镜像流向（排障时先认清失败在哪一跳）：

```
GitHub Actions runner（美国） → GHCR（源制品 + SBOM attestation）
  → aliecs 按 OCI digest 拉取并导出 Docker archive
  → Syncthing 分块续传到 txecs
  → txecs 校验 release 清单后 docker load，以 TCR 名作为本地运行 tag，离线部署

GitHub runner → TCR 仍异步镜像同一 digest，作为 `business-cn-tcr-fallback` 和
bridge 的手工 `bridge-cutover` fallback，不再阻塞 peer-channel 主发布。
```

发布边界：日常只有一条物理镜像通道，但有两个逻辑部署单元。`business-cn` release 固定
包含 6 个业务镜像；`openclaw-bridge` release 固定只含 1 个 bridge 镜像。两者各自维护
release ID、运行状态、回滚和 ACK，避免 bridge 故障冻结业务部署，也避免业务部署重启消息咽喉。

- runner→TCR 是跨境一跳，**只在真正需要实传新镜像时才会暴露问题**：tree hash 命中
  （target 已是同 digest）时秒过，所以「以前一直没事」往往只是没传过。两种失败形态：
  - 快速报错 `dial tcp …:443: i/o timeout`（2026-08-04，同批 7 个挂 1 个，人工 rerun 后过）。
  - **连接建立后完全冻结**（2026-08-05，backend-api / tplus-sync-worker）：日志把
    `Copying blob` 列完就再无任何输出，20 分钟 0 字节，三次重试形态完全一致。
    判断依据就是看 `Copying blob` 之后有没有进度行——没有就是冻结，不是慢，
    **放宽超时毫无意义**。
  - 异步 fallback job 使用 `timeout 25m skopeo copy --retry-times 3`；
    `--retry-times` 只对显式报错有效，外层 timeout 负责终止无输出冻结。
- 这跳失败与 txecs 无关，**txecs 侧的任何中转/代理都帮不上**——它不在这条路径上。
  「让 txecs 直接从 GHCR 拉」这个看似显然的备选**同样不可行**：2026-08-05 实测
  txecs←ghcr.io 握手仅 0.09s 但吞吐只有 **13.6 KB/s**，拉 300MB 镜像要 6 小时。
  这也是它降级为异步备用路径的原因；正常发布不再等待这一步。
- 异步 TCR job 仍属于同一个 workflow；它不阻止本轮 peer 部署和 ACK，但在
  `concurrency: release-deploy` + `cancel-in-progress: false` 下，会延后下一轮 workflow。
  peer ACK 已成功且只剩 TCR fallback 冻结时，可取消该 run 止损，生产不会回滚。
- aliecs 的 sing-box 只是 Chrome→ChatGPT 的自用出口，迁移方案已定性"境内无任何转发
  第三方流量的能力"。2026-07-25 的裁决原文只限定 **devbox 不承担 txecs 文件/镜像中转**，
  不涉及其他设备（此处原先写成"不要改道到 sing-box 或 webdock 节点"，是过度概括，已更正）。
  仍然成立的是：**不要把镜像分发改道成代理转发**（sing-box 转发第三方流量）；
  服务器自己 pull 再传给另一台，是另一回事，见下面的应急旁路。

### 历史应急旁路：aliecs 拉 GHCR → peer-restic SFTP → txecs（2026-08-05 实测打通）

runner→TCR 冻结时用这条，**零新增凭据、零新增 SSH 授权**——完全复用 txecs 每日
Restic 备份用的 `restic-peer` 通道（chroot + `ForceCommand internal-sftp` +
`AllowTcpForwarding no`，拿不到 shell）。

该手工路径现只作 peer-channel 故障回滚。日常主路径使用 `peer-channel` 身份；它复用
同一把主机私钥，但没有 SFTP/文件系统权限，只能转发 rest-server 与 Syncthing 两个
固定 loopback 端口。

```
aliecs ←pull─ GHCR                       12.97 MB/s（aliecs 已长期登录 ghcr.io）
aliecs ─docker save|gzip -1─> /srv/peer-restic/repos/transfer/   （root:755，txecs 只读）
 txecs ─sftp get─ 同一条 peer-restic 通道  2.84 MB/s（200MB/74s，无冻结）
 txecs ─docker load─> docker tag 成 TCR 名 ─> compose up -d
```

要点：

- aliecs 的 SFTP chroot 根是 `/srv/peer-restic`，`ForceCommand internal-sftp -d /repos`，
  所以 txecs 侧看到的路径是 `transfer/xxx`，实际落在 `/srv/peer-restic/repos/transfer/`。
- txecs 侧的 key 是 `/srv/business-cn/config/peer-backup.key`（0600，render.sh 渲染）。
- `docker load` 会校验层 digest；实测 image ID 与 GHCR 源完全一致，传输无损。
- **必须打成 TCR 镜像名**再 up，因为 compose.env 里是 `*_IMAGE=ccr.ccs.tencentyun.com/...`。
- `deploy.sh` 已内置 `ALLOW_OFFLINE_CACHED_IMAGES=true` 离线缓存门（验证本地镜像存在而不 pull），
  走完整 deploy 流程时用它；只换镜像 tag 的最小切换则直接改 compose.env + `compose up -d`。
- 用完清理两侧的中转文件与镜像；aliecs 根分区只有 13G 余量。
- ⚠️ 只换 tag 的最小切换**不写部署清单、不更新 last-success-commit**，CI 侧仍显示未部署；
  网络恢复后重跑一次正式 workflow 即可对齐（tag 相同，容器不会重复重建）。
- 增量部署已实战验证：改 1 个服务只重建 1-2 个容器。
- 纯 Markdown/AGENTS/CLAUDE 变更不触发生产部署。
- 日常版本权威链：Git commit SHA → GitHub Actions run/attempt → OCI image digest。
- 旧 `VYYYYMMDDNNN`/semver 仅保留历史兼容，不再为每次 main 部署生成。
- `FORCE_MIGRATIONS=1` 强制跑迁移。

## 触发语义：push 只构建，部署必须手工 dispatch（2026-08-10 复核 `release-deploy.yml`）

`on.push`（main，忽略 `*.md` / `docs/**`）只跑 `resolve-release` + `build-push` +
`mirror-built-to-tcr`。**所有业务部署 job 都要求 `workflow_dispatch`**——PR 合并进 main
不会部署任何东西，workflow 报 `Success` 只代表镜像构建成功。

| job | 触发条件（2026-08-10 实读） | 是什么 |
|---|---|---|
| `stage-business-cn-peer` | dispatch + `deploy_target=business-cn` | ~~**业务主路径**：aliecs 拉 GHCR → Syncthing → txecs~~ ⚠️ **该说法自 2026-08-16 起失效**（aliecs 停机，peer 通道断），见下方〈aliecs 停机期间主路径已换〉 |
| `deploy-business-cn` | dispatch + `deploy_target=business-cn-tcr-fallback` | ~~⚠️ **名字骗人**：TCR 回退路径。走主路径部署时它**永远 skipped**，据此判断会把成功的部署误判成没部署~~ ⚠️ **判据自 2026-08-16 起反转**：现在它就是主路径，`success` 才算部署成功 |
| `prepare-business-candidate` | dispatch + `deploy_target=business-candidate` | 候选环境 |
| `stage-openclaw-bridge-peer` | push 且 bridge 上下文变化，或 dispatch + `bridge-peer` | bridge 独立发布单元 |
| `mirror-built-to-tcr` | `vars.TCR_BASE != ''`，**push 也跑** | 异步备用镜像，失败不阻塞主发布 |

部署命令：`gh workflow run release-deploy.yml --ref main -f deploy_target=business-cn`

**2026-08-10 实际踩到**：PR #288 合并后 run #376 显示 `Success`、7 个 `build-push` 全绿，
据此报告"已部署"是错的——`stage-business-cn-peer` 压根没被触发。取证发现四个容器仍是
`Up 24 hours`、`curl https://hydwang.xyz/tplus-sync/ | grep -c manualFullSyncBtn` 返回 0。
手工 dispatch 后 `stage-business-cn-peer` success，容器才全部重建、镜像 tag 全变。
同一轮里 `mirror-built-to-tcr (backend-api)` 仍 failure，生产不受影响（见上方 TCR 说明）。

**判定部署成功的唯一可靠方式**（顺序不可省）：

```bash
gh run view <run-id> --json jobs --jq '.jobs[] | select(.name|test("stage-business-cn-peer")) | .conclusion'
ssh txecs "sudo docker ps --format '{{.Names}}\t{{.Status}}\t{{.Image}}'"   # 启动时间要变新、tag 要变
curl -s https://hydwang.xyz/<改动页面> | grep -c '<本次新增的标记>'            # 外部实证
```

## aliecs 停机期间主路径已换：`business-cn-tcr-fallback`（2026-08-16 起，2026-08-28 复核）

> ⚠️ 上一节表格里「`stage-business-cn-peer` 是业务主路径」「`deploy-business-cn` 名字骗人、
> 恒 skipped」两句**自 2026-08-16 起失效**。照旧句走会 dispatch 一个永远等不到 ACK 的 job，
> 并把真正成功的部署判成没部署。旧句保留是因为 aliecs 复机后主路径会换回去。

aliecs 于 2026-08-16 停机，peer 通道（aliecs 拉 GHCR → Syncthing → txecs）随之断掉。
2026-08-28 复核：`ssh aliecs` 直接 `Connection timed out`。

现行部署命令与判据：

```bash
gh workflow run release-deploy.yml --repo huozao/AliECS --ref main \
  -f deploy_target=business-cn-tcr-fallback

# 判据：deploy-business-cn 必须 success；stage-business-cn-peer 此时恒 skipped，属正常
gh run view <run-id> --repo huozao/AliECS --json jobs \
  --jq '[.jobs[]|select(.name|test("deploy-business-cn|stage-business-cn-peer"))|{name,conclusion}]'
```

2026-08-25 那次成功部署（run 32802969490）的实测形态就是
`stage-business-cn-peer=skipped` + `deploy-business-cn=success`。

### 走 TCR 路径时 `mirror-built-to-tcr` 从"可失败"变成硬前置

上一节写着「`mirror-built-to-tcr` 异步备用镜像，失败不阻塞主发布」——那是 peer 路径下的事实。
**走 `business-cn-tcr-fallback` 时，deploy 是按内容标签从 TCR 拉镜像的，镜像没同步过去就拉不到。**

而这个 job 是 `continue-on-error: true`，`deploy-business-cn` 的 `needs` 里也**没有它**，所以：

- 整个 run 的 conclusion 照样是 `success`
- dispatch 部署照样会启动，然后在 txecs 上拉镜像时才炸

2026-08-28 实际踩到：`8943200` 那轮 run 33144981492 整体 `success`，但
`mirror-built-to-tcr` 的 `backend-api` / `doc-sync-worker` / `mcp-coding-server` 三个失败，
失败形态是 skopeo 从 GHCR 读 blob `unexpected EOF` 后卡住、撞满 `timeout 25m` 退出 124：

```
Reading blob body from https://ghcr.io/v2/huozao/backend-api/blobs/sha256:396a0f0e... \
  failed (unexpected EOF), reconnecting after 10485761 bytes…
##[error]Process completed with exit code 124
```

**dispatch 之前必须先核对**：本次内容标签变化的每个服务，它的 `mirror-built-to-tcr` 都得是
`success`。标签变没变这样算（`git rev-parse <sha>:<dir>` 前 12 位就是 `t-` 后面那串）：

```bash
for d in services/public-web services/admin-ui services/backend-api \
         services/doc-sync-worker services/tplus-sync-worker services/mcp-coding-server; do
  old=$(git rev-parse <上次部署的sha>:$d | cut -c1-12)
  new=$(git rev-parse <本次sha>:$d | cut -c1-12)
  [ "$old" = "$new" ] && echo "$d t-$new 未变" || echo "$d t-$old -> t-$new 已变"
done
```

标签未变的服务，TCR 里已有上一轮的同名镜像，它的 mirror 失败可以忽略。
失败的补法：`gh run rerun <run-id> --failed`（只重跑 failed，skipped 的不会被拉起）。

**bridge 的判据是另一套，别套用业务那条。** bridge 在 `deploy/openclaw-bridge/**` 内容变化时
**push 即部署**（不需要 dispatch），走 `stage-openclaw-bridge-peer`；此时
`deploy-business-cn` 和 `stage-business-cn-peer` 都是 skipped，属正常——本轮压根没动业务镜像，
据此判"没部署"是误判。容器侧证据取这两样（2026-08-14 PR #316 实测）：

```bash
gh run view <run-id> --repo huozao/AliECS --json jobs \
  --jq '.jobs[] | select(.name|test("stage-openclaw-bridge-peer")) | .conclusion'
ssh txecs "docker ps --filter name=openclaw-bridge --format '{{.Status}}|{{.CreatedAt}}'"
ssh txecs "grep -i bridge /srv/internal-stack/release.env"
```

`OPENCLAW_BRIDGE_VERSION` 的值形如 `sha-<commit12>@<run-id>/<attempt>`，**run-id 能直接和本次
run 对上**——这是比容器 CreatedAt 更硬的证据（CreatedAt 只能证明"重建过"，证明不了是哪一轮）。

## 同一个 commit 重复部署：ACK 按「每次部署」命名（2026-08-13 修复）

**修改前行为**：`stage-business-cn-peer` 轮询的 ACK 文件按 `release_id`（`sha-<commit前12位>`）
命名。同 commit 第二次 dispatch 时三处同时跳过——aliecs 的 `peer-release-stage` 判定
「already staged」、txecs 的 `peer-release-consume` 判定「ACK 已存在」、workflow 第一圈轮询
直接读到上一次留下的 ACK——于是 1 秒返回 success，容器完全没动。
2026-08-12 实测：run 31566654644 的该步骤 05:29:44 启动，读到的却是 05:15:18 的 ACK，
是 17 分钟前 run 31565751714 写的。同 commit 内容一致，那次没造成实际损失；
**真正会咬人的是改了 runtime env 后重建**——秒绿让人以为已生效，其实容器没动。

**修改后行为**：release request 增加 `deployment_id = sha-<12>-r<run_id>.<attempt>`，
staged 目录和 ACK 都按它命名；workflow 再校验 ACK 里的 `deployment_id`、`github_run_id`、
`github_run_attempt` 与本次 run 相等。同 commit 重投必然是一个新身份，会真正重新 stage、
重新同步、重新部署（txecs 侧重跑 `render.sh` + `deploy-role.sh`）。bridge 路径同样处理。

`release_id` **未改动**，仍是 `sha-<12>`：它同时是 GHCR commit alias 标签和 `deploy-role.sh`
的 IMAGE_TAG，受 `deploy/ecs/deploy.sh` 的 `^(sha-[0-9a-f]{12,40}|v…|V…)$` 校验约束，
把它改成带 run 后缀会被当场打回。

**发布触发语义未变**：仍然只有 `workflow_dispatch` 才部署，push 只构建。

**代价**：同 commit 重投不再复用已 staged 的包，会重新导出并经 Syncthing 重传整套镜像 tar。
`peer-release-stage.sh` 的保留策略（每类型保留 2 个已 ACK 的 release）和 6 GiB 空间下限不变。

**上线顺序**：infra 的 `peer-release-stage.sh` / `peer-release-consume.sh` 必须**先**发到
aliecs 与 txecs。两个脚本对缺 `deployment_id` 的旧 request 会回落到 `release_id`，
所以先上 infra 不改变任何现有行为；顺序反了则 workflow 找不到新命名的 ACK，会卡满 30 分钟超时。

**回退方式**：revert AliECS 这个 PR 即可，infra 侧向后兼容不必同时回退。

## 症状表

| 症状 | 先查 | 处置 |
|---|---|---|
| 全站/部分页面 502 | `ssh txecs "sudo docker ps -a --format '{{.Names}}\t{{.Status}}'"` 找 **Created/Exited** 容器（只查 backend healthz 会漏静态容器） | 容器已是新镜像只是没启动 → 先查本次 deploy log 和运行 profile；不要盲目整体重跑 |
| 部署 job 报 Timeout/failed | 先看容器是否已是新版本 tag | 已新版**别重跑**（重跑=再拉镜像有 OOM 风险）；漏启的逐个 `docker start` |
| ECS 整机假死（CPU100%+磁盘纯读+ssh banner 超时） | OOM thrashing 特征 | 根因已修（swappiness=20 + deploy.sh 串行 pull）；卡死>10min 且 TCP 通应用层死 → 阿里云控制台强制重启是安全的（容器 restart:always） |
| backend unhealthy | healthcheck `start_period: 300s` 内属正常；healthz 对可选探测必须容错（惰性目录事故 PR#106） | 等 start_period；真不健康看 `docker logs` |
| 部署后页面没变 | 内容寻址：内容没变的服务标签不变、容器不重建 | 确认改动落在对应构建上下文目录内 |
| txecs 的三个 worker 同时 Exited(137)，随后部署仍不启动 | `sudo grep -E '^(P0_MODE|TPLUS_BOM_WRITE_ENABLED)=' /srv/business-cn/config/runtime.env` | 正式 `business-cn` workflow 必须用 infra 的 production profile：`P0_MODE=false`、`TPLUS_BOM_WRITE_ENABLED=true`。P0 profile 会主动停 worker，只用于隔离阶段 |
| 宿主机 `127.0.0.1` 上游正常，但 backend 容器访问 `host.docker.internal` 超时 | 从 `business-cn-backend-api-1` 内分别探 11800/18080/18200/18201，再查 `172.17.0.1` listener 与 UFW 的 `172.18.0.0/16` 来源限制 | `install-host-gateway-proxies.sh` 管 18080/18200/18201，旧的 `webdock-tunnel-proxy.service` 管 11800；两边都必须同时具备 proxy listener 和窄 UFW 规则。不要把端口直接发布到公网 |
| workflow 显示 success，但生产没变（容器还是旧的 Up N days） | 先看**是不是 push 触发的**（push 不部署，见上方「触发语义」）；再看 `stage-business-cn-peer` 的结论 | 两种成因：①（2026-08-10）push/合并根本不触发部署，需手工 dispatch；②（2026-08-04）`gh run rerun --failed` **只重跑 failed 的 job，skipped 的不会被拉起**，build 失败 → deploy 被 skip → 重跑后 build 转绿、conclusion=success、部署却没执行。两者处置相同：`gh workflow run release-deploy.yml --ref main -f deploy_target=business-cn` 跑完整一轮。⚠️ 判据是 `stage-business-cn-peer`，**不是 `deploy-business-cn`**（后者是 TCR fallback，主路径下恒为 skipped） |
| `mirror-built-to-tcr` 失败或超时 | `stage-business-cn-peer` 和 txecs ACK 是否成功 | peer 成功则生产发布已完成，TCR 仅备用镜像缺一版；网络恢复后重跑 `mirror-only`。只有显式 `business-cn-tcr-fallback` 才被它阻塞 |
| `stage-business-cn-peer` 等不到 ACK | aliecs `peer-syncthing`、txecs tunnel/syncthing/consume timer；两侧 `release.json` 与 ACK | 不手工跳过哈希门。先修同步；需止损时显式选择 `business-cn-tcr-fallback` |
| `stage-openclaw-bridge-peer` 等不到 ACK | 先按上一行查同一物理通道，再看 bridge release 的 ACK、`txecs-openclaw-bridge.service` 和 `/v1/models` | 修复后手工选择 `bridge-peer` 重发；紧急回退才使用 `bridge-cutover.yml` 的 TCR 路径。不要改成业务 release，也不要重启 business-cn |
| 手工 TCR `bridge-cutover` 失败，日志是 `ssh: handshake failed: EOF` | 本机 `ssh txecs` 同时也会间歇 `Connection closed`。查 `sudo journalctl -u ssh` 是否出现 `beginning MaxStartups throttling` / `drop connection`，再查 `sudo fail2ban-client status sshd` | txecs 公网 22 持续遭遇机会型自动扫描；来源 IP 多登记在腾讯云网段，但不能据此断言主机归属或是否已被入侵。2026-07-28 曾触发 `MaxStartups 10:30:100` 并丢弃合法连接；同日已启用 fail2ban sshd aggressive jail（5 次/10 分钟、封 24 小时），未放宽 `MaxStartups`。确认扫描源已被封且本机 SSH 稳定后，再重跑 fallback。密码和 root 登录均关闭；成功登录还必须按用户、来源 IP、密钥指纹核对，不能只看 IP |

<!-- nav-check: deploy/ecs/install-host-gateway-proxies.sh -->

## 验证命令

```bash
ssh txecs "sudo docker ps --format '{{.Names}}\t{{.Status}}'"
curl -s -o /dev/null -w '%{http_code}' https://hydwang.xyz/formula/   # 外部真验证
bash -n deploy/ecs/{deploy,migrate,healthcheck,rollback}.sh            # 改脚本后
bash deploy/ecs/tests/test_deploy_roles.sh                              # role/profile 守卫
```

⚠️ `gh run watch` 会提前报 success，以部署日志和外部 curl 为准。

## 热补丁与迁移手法（紧急时）

```bash
# 原子改容器内文件（temp+mv，restart 保留写层；compose up/recreate 会回镜像丢补丁）
ssh txecs 'sudo docker exec -i <容器> sh -c "cat > /path.new && mv /path.new /path"' < 本地文件
# 跑迁移（写成幂等：IF NOT EXISTS / ON CONFLICT DO NOTHING）
ssh txecs 'sudo docker exec -i business-cn-postgres-1 psql -U app -d app -v ON_ERROR_STOP=1' < db/migrations/xxx.sql
```

⛔ **热补丁验证成功后必须回灌 git**（AliECS 走 PR），否则 release 重建覆盖丢失。

## 回滚

默认 `deploy/ecs/rollback.sh` 回上一版本；指定
`deploy/ecs/rollback.sh <deployment_id>` 可按 `$METADATA_DIR/deployments/<id>.json`
中的不可变 digest 回滚。现有 V tag 不删除，仅作历史兼容。
