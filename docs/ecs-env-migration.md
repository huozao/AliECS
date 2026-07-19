# ECS 环境变量迁移步骤

## 先说结论

本地 `local/.env.local` 只用于本地验证，不直接提交 GitHub，也不建议原样当生产配置。ECS 生产环境使用：

- `/root/AliECS/deploy/ecs/release-meta.env`：人工维护的私有生产配置。
- `/root/AliECS/deploy/ecs/runtime.env`：由 `deploy.sh` 根据 `release-meta.env` 生成，供 `compose.prod.yml` 使用。

不要把真实密码、企业微信 secret、GitHub PAT、SSH 私钥写入仓库。

## 1. 在本地准备生产配置文件

```powershell
Copy-Item deploy\ecs\release-meta.env.example C:\tmp\aliecs-release-meta.env
```

编辑 `C:\tmp\aliecs-release-meta.env`，至少填写 `APP_ROOT`、`COMPOSE_FILE`、`RUNTIME_ENV_FILE`、`POSTGRES_*`、`DATABASE_URL`、`AUTH_TOKEN_SECRET`、管理员初始账号、`GHCR_BASE`。如果 GHCR 镜像是 private，还要填写 `GHCR_USERNAME` 和只具备 `read:packages` 权限的 `GHCR_TOKEN`。

## 2. 填写企微同步配置

```dotenv
WECOM_ENV_PROFILES=COMPANY_A,COMPANY_B

WECOM_COMPANY_A_CORP_ID=
WECOM_COMPANY_A_APP_SECRET=
WECOM_COMPANY_A_APP_SECRET_2=
WECOM_COMPANY_A_APP_SECRETS=
WEDOC_COMPANY_A_DOCID=
WEDOC_COMPANY_A_SHEET_ID=
WEDRIVE_COMPANY_A_SPACEIDS=
SMARTSHEET_COMPANY_A_ID=
SMARTSHEET_COMPANY_A_SHEET_ID=

WECOM_COMPANY_B_CORP_ID=
WECOM_COMPANY_B_APP_SECRET=
WECOM_COMPANY_B_APP_SECRET_2=
WECOM_COMPANY_B_APP_SECRETS=
WEDOC_COMPANY_B_DOCID=
WEDOC_COMPANY_B_SHEET_ID=
WEDRIVE_COMPANY_B_SPACEIDS=
SMARTSHEET_COMPANY_B_ID=
SMARTSHEET_COMPANY_B_SHEET_ID=
```

`WEDOC_*_DOCID` 和 `SMARTSHEET_*_ID` 可以写多个，用英文逗号或分号分隔。worker 会按 docid 获取所有 sheet，并分页同步所有 records。

## 3. 复制到 ECS

PowerShell：

```powershell
scp C:\tmp\aliecs-release-meta.env root@你的ECS公网IP:/root/AliECS/deploy/ecs/release-meta.env
```

ECS 上收紧权限：

```bash
ssh root@你的ECS公网IP
chmod 600 /root/AliECS/deploy/ecs/release-meta.env
```

## 4. 检查变量名，不打印密钥值

```bash
cd /root/AliECS
grep -E '^(APP_ROOT|COMPOSE_FILE|RUNTIME_ENV_FILE|DATABASE_URL|WECOM_ENV_PROFILES|WECOM_.*_CORP_ID|WECOM_.*_APP_SECRET|WEDOC_.*_DOCID|SMARTSHEET_.*_ID)=' deploy/ecs/release-meta.env | sed 's/=.*/=***/'
```

## 5. 部署并生成 runtime.env

```bash
cd /root/AliECS/deploy/ecs
./deploy.sh sha-<Git commit 前12位>
```

验证：

```bash
docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml ps
curl -fsS http://127.0.0.1:8000/healthz
```

## 6. 在 ECS 上运行企微同步

完整同步：

```bash
docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main sync-wecom-full
```

消费 Admin UI 创建的手动同步请求：

```bash
docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main consume-sync-requests --limit 10
```

同步指定 source：

```bash
docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main sync-wecom-source --source-id 1
```

## 7. 建议配置定时任务

```bash
crontab -e
```

加入：

```cron
*/15 * * * * cd /root/AliECS && docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main consume-sync-requests --limit 10 >> /root/AliECS/deploy/ecs/logs/doc-sync-requests.log 2>&1
0 */2 * * * cd /root/AliECS && docker compose --env-file /root/AliECS/deploy/ecs/runtime.env -f /root/AliECS/deploy/ecs/compose.prod.yml run --rm doc-sync-worker python -m app.main sync-wecom-full >> /root/AliECS/deploy/ecs/logs/doc-sync-full.log 2>&1
```

## 常见问题

- `errcode 60020`：检查企业微信可信 IP，把 ECS 出口公网 IP 加入应用可信 IP。
- `invalid docid`：确认 `WEDOC_COMPANY_A_DOCID` 或 `SMARTSHEET_COMPANY_A_ID` 是智能表格 docid。
- `no access`：确认企业微信应用有该智能表格权限。
- `relation ... does not exist`：执行部署脚本或迁移脚本，确认 `db/migrations/0005_doc_sync.sql` 已应用。
- Admin UI 看不到数据：先运行一次 `sync-wecom-full`，或确认 `external_sources` 中已有同步源。
