# GitHub 到 ECS 自动部署基础流程

## 目标

日常发布只走一条简单路径：

1. Codex 或人工提交代码到 GitHub 分支。
2. 创建 PR 并合并到 `main`。
3. GitHub Actions 构建镜像并推送到 GHCR。
4. GitHub Actions 通过 SSH 登录 ECS。
5. ECS 自动同步 `/root/AliECS` 到最新 `origin/main`。
6. ECS 保护并恢复私有环境变量文件。
7. ECS 执行最新 `deploy/ecs/deploy.sh` 完成部署。

正常情况下，不需要人工 SSH 到 ECS 执行 `git pull`。

## ECS 私有配置

以下文件是 ECS 私有运行配置，不提交 GitHub：

- `/root/AliECS/deploy/ecs/release-meta.env`
- `/root/AliECS/deploy/ecs/runtime.env`

`release-meta.env` 由人工在 ECS 上维护，保存数据库密码、登录密钥、GHCR 拉取凭证、企微凭证等真实值。

`runtime.env` 由 `deploy.sh` 根据 `release-meta.env` 生成。通常不要手工长期修改它。

## 自动部署时如何保护 env

GitHub Actions 的 SSH 部署步骤会：

1. 备份 ECS 上的 `release-meta.env` 和 `runtime.env` 到 `/root/AliECS-private-backup/github-actions`。
2. 执行 `git fetch origin main --tags`。
3. 切到 `main`。
4. 恢复历史上被 Git 跟踪过的 env 文件，避免本地生产配置阻断 `git pull --ff-only`。
5. 执行 `git pull --ff-only origin main`。
6. 把备份的 ECS 私有 env 文件复制回 `/root/AliECS/deploy/ecs/`。
7. 执行最新的 `deploy.sh`。

这样 `compose.prod.yml`、`deploy.sh`、数据库迁移和新服务定义会随 GitHub main 自动更新，而真实密钥不会进入仓库。

## 首次或异常恢复

如果 ECS 上 `/root/AliECS` 不是 Git 仓库，自动部署会失败并提示路径错误。需要先在 ECS 上准备一次仓库目录。

如果 `git pull --ff-only` 失败，说明 ECS 本地除了私有 env 外还有其它被修改的跟踪文件。先看：

```bash
cd /root/AliECS
git status -sb
```

不要直接执行 `git reset --hard`，除非确认这些本地改动都不需要保留。

## 部署后验证

```bash
cd /root/AliECS

grep -n "doc-sync-worker" deploy/ecs/compose.prod.yml
ls db/migrations/0005_doc_sync.sql

docker compose --env-file /root/AliECS/deploy/ecs/runtime.env \
  -f /root/AliECS/deploy/ecs/compose.prod.yml \
  ps
```

企微同步：

```bash
docker compose --env-file /root/AliECS/deploy/ecs/runtime.env \
  -f /root/AliECS/deploy/ecs/compose.prod.yml \
  run --rm doc-sync-worker python -m app.main sync-wecom-full
```
