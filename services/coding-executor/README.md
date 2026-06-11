# coding-executor（开发机本地服务，阶段二 dry-run）

ChatGPT 连接器 → ECS `mcp-coding-server` → **反向 SSH 隧道** → 本服务（开发机）。
本服务只对白名单仓库执行**只读** git 操作，绝不修改文件。

## 它不是什么

- 不进 ECS、不进 `compose.prod.yml`、不进 release 构建矩阵。它只跑在开发机。
- 阶段二只读：`git_status` / `git_log` / `git_diff` / `list_files` / `read_file`。

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

```bash
ssh -N -R 127.0.0.1:18091:127.0.0.1:18091 aliecs
```

ECS 端 `mcp-coding-server` 用 `EXECUTOR_BASE_URL=http://host.docker.internal:18091`
经隧道回连。隧道或本服务未启动时，ChatGPT 侧工具会优雅降级为 `executor: unavailable`。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查（免鉴权） |
| GET | `/repos` | 仓库白名单 + 允许的操作 |
| POST | `/tasks` | 发起任务 `{repo, action, params}` → `{id, status}` |
| GET | `/tasks/{id}` | 查询任务状态与结果 |
