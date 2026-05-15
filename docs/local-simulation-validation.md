# 本地模拟验证环境

## 目的

本地模拟验证用于让 Codex 和开发者在本机启动 `public-web`、`admin-ui`、`backend-api` 和本地 PostgreSQL，验证页面、接口、登录和健康检查是否正常。

这套流程只服务本地开发和排查，不执行生产部署，不连接 ECS 生产数据库，不依赖生产密钥。

## 安全边界

不要把生产服务器密码复制到本地。原因是本地调试会产生日志、命令历史、容器环境变量和临时文件，一旦混入真实生产密码、SSH 私钥、GitHub PAT 或阿里云 AccessKey，就会扩大泄露面。

本地验证只能使用：

- `local/docker-compose.local.yml`
- `local/.env.local`
- 本地 Docker Compose
- 本地 PostgreSQL
- 本地测试账号
- 本地假密钥

不要读取或复用：

- `deploy/ecs/runtime.env`
- `deploy/ecs/release-meta.env`
- 服务器生产数据库连接串
- SSH 私钥
- GitHub PAT
- 阿里云 AccessKey

## 准备本地变量

复制示例文件：

```bash
cp local/.env.local.example local/.env.local
```

Windows PowerShell：

```powershell
Copy-Item local/.env.local.example local/.env.local
```

`local/.env.local` 只能填写本地测试值，不得提交 GitHub。

默认测试管理员账号：

- username: `admin`
- password: `admin123`

这只是本地测试账号，不得用于生产。

## 运行验证

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/local-smoke-test.ps1
```

Linux/macOS：

```bash
bash scripts/local-smoke-test.sh
```

脚本会执行：

```bash
docker compose -f local/docker-compose.local.yml config
docker compose -f local/docker-compose.local.yml up --build -d
docker compose -f local/docker-compose.local.yml ps
```

如果 `local/.env` 或 `local/.env.local` 里已经填了真实测试凭证，手工运行 `config` 时建议重定向输出：

```bash
docker compose -f local/docker-compose.local.yml config >/dev/null
```

原因是 Docker Compose 会展开环境变量，直接打印完整配置可能把凭证显示在终端日志里。项目自带 smoke test 脚本已经使用静默配置校验。

并检查：

- public-web: http://localhost:8080
- admin-ui: http://localhost:8081
- backend-api: http://localhost:8000
- healthz: http://localhost:8000/healthz

如果 `http://localhost:8000/api/healthz` 不存在，脚本会说明这是可选检查失败，不会误判整个验证失败。

## 常见问题

### local/.env.local 缺失

按文档复制示例：

```bash
cp local/.env.local.example local/.env.local
```

### Docker 没启动

启动 Docker Desktop 或 Docker 服务后重试。

### 端口被占用

检查 `8080`、`8081`、`8000` 是否已被占用。关闭占用进程或先清理旧容器。

### postgres volume 里保留了旧数据

普通清理不会删除数据库卷。如果需要重置本地数据库，使用带 `-v` 的清理命令。

### healthcheck 失败

查看脚本输出的最近日志，重点检查：

- `DATABASE_URL`
- PostgreSQL 是否启动成功
- 数据库迁移是否初始化
- backend-api 是否能连接 postgres

## 清理命令

保留数据库卷：

```bash
docker compose -f local/docker-compose.local.yml down
```

删除本地数据库卷并重置数据：

```bash
docker compose -f local/docker-compose.local.yml down -v
```

## Doc Sync 本地验证补充

如果你的本地测试变量不在 `local/.env.local`，Windows 可以显式传入 env 文件：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/local-smoke-test.ps1 -EnvFilePath C:\path\to\.env
```

Linux/macOS 可以传入：

```bash
bash scripts/local-smoke-test.sh --env-file /path/to/.env
```

如果要真实调用企业微信同步，env 文件需要至少包含这些变量名：

- `WECOM_ENV_PROFILES`
- `WECOM_<PROFILE>_CORP_ID`
- `WECOM_<PROFILE>_APP_SECRET`
- `WEDOC_<PROFILE>_DOCID` 或 `SMARTSHEET_<PROFILE>_ID`

Windows 运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/local-smoke-test.ps1 -RunDocSync
```

Linux/macOS 运行：

```bash
bash scripts/local-smoke-test.sh --run-doc-sync
```

脚本不会打印密钥值；如果缺少企微变量，会跳过真实同步，只验证容器、接口、登录和 worker 命令。
