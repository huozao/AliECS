# Runbook：部署 / CI / 回滚 / 502 排障

## 链路图

```
push main（PR 合并）
  → GitHub Actions build-push：镜像标签 = 目录 git tree hash（t-xxxxxxxxxxxx），
    manifest 命中即跳过构建；sha-<commit> 只是同批次 GitHub 别名
  → 获授权后 workflow_dispatch 选择 business-cn
  → ssh deploy 到 txecs（appleboy，command_timeout 40m）
  → /srv/business-cn/current：按目录导出 *_TAG → 迁移（db/ 未变更即跳过）
  → docker compose up -d（内容没变的服务不重建容器）
  → 三个生产 worker 运行守卫 → healthcheck
  → 写 last-success-commit + 部署清单（commit/run/attempt/各镜像 digest）
```

镜像流向（排障时先认清失败在哪一跳）：

```
GitHub Actions runner（美国）
  → build-push：构建后 skopeo copy 到 GHCR 和腾讯云 TCR(ccr.ccs.tencentyun.com)
  → txecs 只从 TCR 拉，26 个镜像全部来自 TCR，不直连 GHCR/Docker Hub
```

- runner→TCR 是跨境一跳，**只在真正需要实传新镜像时才会暴露问题**：tree hash 命中
  （target 已是同 digest）时秒过，所以「以前一直没事」往往只是没传过。两种失败形态：
  - 快速报错 `dial tcp …:443: i/o timeout`（2026-08-04，同批 7 个挂 1 个，人工 rerun 后过）。
  - **连接建立后完全冻结**（2026-08-05，backend-api / tplus-sync-worker）：日志把
    `Copying blob` 列完就再无任何输出，20 分钟 0 字节，三次重试形态完全一致。
    判断依据就是看 `Copying blob` 之后有没有进度行——没有就是冻结，不是慢，
    **放宽超时毫无意义**。
  - 已加护栏（PR#272）：`timeout 20m skopeo copy --retry-times 3`，最多 3 轮。
    `--retry-times` 只对报错有效，对冻结无效，必须靠 `timeout` 把冻结转成失败才会重试。
    因为已确认是冻结而非慢传输，20m 偏宽（每次白等 60 分钟才失败），可下调到 8-10m。
- 这跳失败与 txecs 无关，**txecs 侧的任何中转/代理都帮不上**——它不在这条路径上。
  「让 txecs 直接从 GHCR 拉」这个看似显然的备选**同样不可行**：2026-08-05 实测
  txecs←ghcr.io 握手仅 0.09s 但吞吐只有 **13.6 KB/s**，拉 300MB 镜像要 6 小时。
  跨境劣化时（实测下午到傍晚）唯一有效的办法是**换时段重跑**，不要改架构。
- 跨境卡死会连带堵住整条发布链：`concurrency: release-deploy` + `cancel-in-progress: false`，
  卡住的 run 会让后面排队的部署一起等。确认是冻结后可直接 `gh run cancel` 止损。
- aliecs 的 sing-box 只是 Chrome→ChatGPT 的自用出口，迁移方案已定性"境内无任何转发
  第三方流量的能力"。2026-07-25 的裁决原文只限定 **devbox 不承担 txecs 文件/镜像中转**，
  不涉及其他设备（此处原先写成"不要改道到 sing-box 或 webdock 节点"，是过度概括，已更正）。
  仍然成立的是：**不要把镜像分发改道成代理转发**（sing-box 转发第三方流量）；
  服务器自己 pull 再传给另一台，是另一回事，见下面的应急旁路。

### 应急旁路：aliecs 拉 GHCR → peer-restic 通道 → txecs（2026-08-05 实测打通）

runner→TCR 冻结时用这条，**零新增凭据、零新增 SSH 授权**——完全复用 txecs 每日
Restic 备份用的 `restic-peer` 通道（chroot + `ForceCommand internal-sftp` +
`AllowTcpForwarding no`，拿不到 shell）。

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

## 症状表

| 症状 | 先查 | 处置 |
|---|---|---|
| 全站/部分页面 502 | `ssh txecs "sudo docker ps -a --format '{{.Names}}\t{{.Status}}'"` 找 **Created/Exited** 容器（只查 backend healthz 会漏静态容器） | 容器已是新镜像只是没启动 → 先查本次 deploy log 和运行 profile；不要盲目整体重跑 |
| 部署 job 报 Timeout/failed | 先看容器是否已是新版本 tag | 已新版**别重跑**（重跑=再拉镜像有 OOM 风险）；漏启的逐个 `docker start` |
| ECS 整机假死（CPU100%+磁盘纯读+ssh banner 超时） | OOM thrashing 特征 | 根因已修（swappiness=20 + deploy.sh 串行 pull）；卡死>10min 且 TCP 通应用层死 → 阿里云控制台强制重启是安全的（容器 restart:always） |
| backend unhealthy | healthcheck `start_period: 300s` 内属正常；healthz 对可选探测必须容错（惰性目录事故 PR#106） | 等 start_period；真不健康看 `docker logs` |
| 部署后页面没变 | 内容寻址：内容没变的服务标签不变、容器不重建 | 确认改动落在对应构建上下文目录内 |
| txecs 的三个 worker 同时 Exited(137)，随后部署仍不启动 | `sudo grep -E '^(P0_MODE|TPLUS_BOM_WRITE_ENABLED)=' /srv/business-cn/config/runtime.env` | 正式 `business-cn` workflow 必须用 infra 的 production profile：`P0_MODE=false`、`TPLUS_BOM_WRITE_ENABLED=true`。P0 profile 会主动停 worker，只用于隔离阶段 |
| workflow 显示 success，但生产没变（容器还是旧的 Up N days） | **`deploy-*` job 的结论是不是 `skipped`**，而不是只看 workflow 总结论 | ⚠️ `gh run rerun --failed` **只重跑 failed 的 job，skipped 的不会被拉起**。build 失败 → deploy 被 skip → 重跑后 build 转绿、workflow conclusion=success，部署却压根没执行（2026-08-04 实际踩到）。正确处置：`gh workflow run release-deploy.yml --ref main -f deploy_target=business-cn` 重跑完整一轮。判定成功必须落到 `deploy-business-cn` 的 job 结论 + `docker ps` 的容器启动时间，两个都看 |
| build-push 挂着不动，步骤停在 `Sync the same immutable manifest to TCR` | 日志里 `Copying blob` 之后有没有进度行 | 没有进度＝跨境冻结（见上）。等 3 轮重试自然失败，或 `gh run cancel` 止损，**换时段重跑**；不要改镜像流向，也不要指望 txecs 侧中转 |
| 8 个 build-push 全 success，只有 `cutover-bridge / cutover` 失败，日志是 `ssh: handshake failed: EOF` | 本机 `ssh txecs` 同时也会间歇 `Connection closed`。查 `sudo journalctl -u ssh` 是否出现 `beginning MaxStartups throttling` / `drop connection`，再查 `sudo fail2ban-client status sshd` | txecs 公网 22 持续遭遇机会型自动扫描；来源 IP 多登记在腾讯云网段，但不能据此断言主机归属或是否已被入侵。2026-07-28 曾触发 `MaxStartups 10:30:100` 并丢弃合法连接；同日已启用 fail2ban sshd aggressive jail（5 次/10 分钟、封 24 小时），未放宽 `MaxStartups`。确认扫描源已被封且本机 SSH 稳定后，再用 `gh run rerun <id> --failed` 重跑。密码和 root 登录均关闭；成功登录还必须按用户、来源 IP、密钥指纹核对，不能只看 IP |

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
