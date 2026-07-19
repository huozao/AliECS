# Runbook：部署 / CI / 回滚 / 502 排障

## 链路图

```
push main（PR 合并）
  → GitHub Actions build-push：镜像标签 = 目录 git tree hash（t-xxxxxxxxxxxx），
    manifest 命中即跳过构建；sha-<commit> 只是同批次 GitHub 别名
  → ssh deploy（appleboy，command_timeout 25m）
  → ECS /root/AliECS：git pull → 按目录导出 *_TAG → 迁移（db/ 未变更即跳过）
  → docker compose up -d（内容没变的服务不重建容器）
  → healthcheck → 写 last-success-commit + 部署清单（commit/run/attempt/各镜像 digest）
```

- 增量部署已实战验证：改 1 个服务只重建 1-2 个容器。
- 纯 Markdown/AGENTS/CLAUDE 变更不触发生产部署。
- 日常版本权威链：Git commit SHA → GitHub Actions run/attempt → OCI image digest。
- 旧 `VYYYYMMDDNNN`/semver 仅保留历史兼容，不再为每次 main 部署生成。
- `FORCE_MIGRATIONS=1` 强制跑迁移。

## 症状表

| 症状 | 先查 | 处置 |
|---|---|---|
| 全站/部分页面 502 | `ssh aliecs "docker ps -a --format '{{.Names}}\t{{.Status}}'"` 找 **Created/Exited** 容器（只查 backend healthz 会漏静态容器） | 容器已是新镜像只是没启动 → `docker start <容器>`（最小动作），**不要整体重跑部署** |
| 部署 job 报 Timeout/failed | 先看容器是否已是新版本 tag | 已新版**别重跑**（重跑=再拉镜像有 OOM 风险）；漏启的逐个 `docker start` |
| ECS 整机假死（CPU100%+磁盘纯读+ssh banner 超时） | OOM thrashing 特征 | 根因已修（swappiness=20 + deploy.sh 串行 pull）；卡死>10min 且 TCP 通应用层死 → 阿里云控制台强制重启是安全的（容器 restart:always） |
| backend unhealthy | healthcheck `start_period: 300s` 内属正常；healthz 对可选探测必须容错（惰性目录事故 PR#106） | 等 start_period；真不健康看 `docker logs` |
| 部署后页面没变 | 内容寻址：内容没变的服务标签不变、容器不重建 | 确认改动落在对应构建上下文目录内 |

## 验证命令

```bash
ssh aliecs "docker ps --format '{{.Names}}\t{{.Status}}'"
curl -s -o /dev/null -w '%{http_code}' https://hydwang.xyz/formula/   # 外部真验证
bash -n deploy/ecs/{deploy,migrate,healthcheck,rollback}.sh            # 改脚本后
```

⚠️ `gh run watch` 会提前报 success，以部署日志和外部 curl 为准。

## 热补丁与迁移手法（紧急时）

```bash
# 原子改容器内文件（temp+mv，restart 保留写层；compose up/recreate 会回镜像丢补丁）
ssh aliecs 'docker exec -i <容器> sh -c "cat > /path.new && mv /path.new /path"' < 本地文件
# 跑迁移（写成幂等：IF NOT EXISTS / ON CONFLICT DO NOTHING）
ssh aliecs 'docker exec -i ecs-postgres-1 psql -U app -d app -v ON_ERROR_STOP=1' < db/migrations/xxx.sql
```

⛔ **热补丁验证成功后必须回灌 git**（AliECS 走 PR），否则 release 重建覆盖丢失。

## 回滚

默认 `deploy/ecs/rollback.sh` 回上一版本；指定
`deploy/ecs/rollback.sh <deployment_id>` 可按 `$METADATA_DIR/deployments/<id>.json`
中的不可变 digest 回滚。现有 V tag 不删除，仅作历史兼容。
