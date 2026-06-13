# coding-executor（开发机本地服务，阶段三 a：worktree 隔离写入）

ChatGPT 连接器 → ECS `mcp-coding-server` → **反向 SSH 隧道** → 本服务（开发机）。

本服务对白名单仓库支持两类操作：

- **只读**（直接在仓库工作区执行）：`git_status` / `git_log` / `git_diff` /
  `list_files` / `read_file`。
- **写入**（仅在隔离 worktree 中执行，分支名 `codex-task-<task_id>`）：
  `write_file` / `apply_patch` / `git_commit` / `git_diff_worktree`。

写操作绝不直接修改用户当前签出的分支，也绝不自动 `push` 或 `merge`。调用顺序：

1. `POST /worktrees {"repo", "task_id", "base_ref"}` 创建隔离 worktree。
2. `POST /tasks {"repo", "action": "write_file"|..., "params": {"task_id": ..., ...}}`
   在该 worktree 中执行写操作。
3. `POST /tasks {"repo", "action": "git_diff_worktree", "params": {"task_id": ...}}`
   或 `GET /tasks/{id}` 查看结果，供人工审阅。
4. `DELETE /worktrees/{repo}/{task_id}` 丢弃 worktree 和分支（不可恢复），或保留
   分支供人工 `git fetch`/`cherry-pick`。

## 它不是什么

- 不进 ECS、不进 `compose.prod.yml`、不进 release 构建矩阵。它只跑在开发机。
- 不是自动编程 agent：每次写操作都是 ChatGPT 显式发起的单步操作，由人工通过
  `git_diff_worktree` 审阅；本阶段不包含无人值守的多步编辑循环（见项目计划中的
  "Phase 3b" 备注）。
- 不会修改用户当前签出的分支，不会 `push`，不会 `merge`。

## 本地运行

```powershell
# 1. 安装依赖
python -m pip install -r services/coding-executor/requirements.txt

# 2. 配置环境变量（PowerShell）
$env:EXECUTOR_TOKEN = "<与 ECS 端一致的随机长串>"
$env:EXECUTOR_REPOS = "aliecs=C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS"
# 多个仓库用分号分隔：name1=path1;name2=path2

# 3. 启动（默认 127.0.0.1:18091）
python -m app.main
```

工作目录需为 `services/coding-executor`，或把该目录加入 `PYTHONPATH`。

## 鉴权

每个请求需带 `Authorization: Bearer $EXECUTOR_TOKEN`。`EXECUTOR_TOKEN` 为空时服务
拒绝所有请求（fail closed）。也可用 `EXECUTOR_TOKEN_FILE` 指向一个只含 token 的文件。

## 反向隧道（开发机 → ECS）

完整数据通路：

```text
mcp-coding-server（ECS 容器, bridge 网络）
  -> host.docker.internal:18091  (= 172.17.0.1)
  -> executor-tunnel-proxy        (ECS, 172.17.0.1:18091 -> 127.0.0.1:18091)
  -> 反向 SSH 隧道                  (ECS 127.0.0.1:18091 -> 开发机 127.0.0.1:18091)
  -> coding-executor              (开发机)
```

容器走 `host.docker.internal`（= docker 网关 172.17.0.1），而反向 SSH 隧道只绑
`127.0.0.1`，两者够不着，所以中间需要一个代理把网关地址转到 loopback。这与 webdock
隧道完全同构，复用同一个 `webdock-tunnel-proxy.py`。

**1. ECS 端一次性安装代理（runtime ops，幂等）：**

```bash
ssh aliecs 'cd /root/AliECS/deploy/ecs && sudo bash install-executor-tunnel-proxy.sh'
```

**2. 开发机起反向隧道（用已有的 aliecs SSH 访问即可）：**

```bash
ssh -N -R 127.0.0.1:18091:127.0.0.1:18091 aliecs
```

**3. ECS 端 `mcp-coding-server` 配 `EXECUTOR_BASE_URL=http://host.docker.internal:18091`
与 `EXECUTOR_TOKEN`（两端一致）。**

隧道、代理或本服务任一未就绪时，ChatGPT 侧工具会优雅降级为 `executor: unavailable`，
不阻塞、不报错。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查（免鉴权） |
| GET | `/repos` | 仓库白名单 + 只读/写操作列表 |
| POST | `/worktrees` | 创建隔离 worktree `{repo, task_id, base_ref}` → `{repo, task_id, branch, path}` |
| DELETE | `/worktrees/{repo}/{task_id}` | 丢弃 worktree 及分支（不可恢复） |
| POST | `/tasks` | 发起任务 `{repo, action, params}` → `{id, status}`；写操作的 `params` 必须含 `task_id` |
| GET | `/tasks/{id}` | 查询任务状态与结果 |
