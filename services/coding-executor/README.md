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
| GET | `/repos` | 仓库白名单 + 允许的操作 |
| POST | `/tasks` | 发起任务 `{repo, action, params}` → `{id, status}` |
| GET | `/tasks/{id}` | 查询任务状态与结果 |
