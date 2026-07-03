# SSO 切换 runbook（Authelia 统一登录）

前置状态（2026-07-03 已完成）：aliecs 上 `sso-authelia` + `sso-lldap` 已运行，
`https://auth.hydwang.xyz` discovery/登录页 200，测试用户 `ssotest` 已建
（lldap `website_users` 组）。backend OIDC 代码在 PR 分支
`feature/unified-account-system`，env 已写入 release-meta.env（OIDC_ENABLED=true）。

## 1. 网站用户建号（lldap）

- `ssh aliecs -L 17170:127.0.0.1:17170` → 浏览器 `http://localhost:17170`
  （admin / 密码 = `sops -d infra/secrets/sso.enc.env` 的 `LLDAP_LDAP_USER_PASS`）。
- 对照 users 表逐个建号：**lldap 用户名 = 网站 username（一字不差，绑定键）**；
  加入 `website_users` 组；设初始密码线下分发。
- 查现网用户清单：
  `ssh aliecs 'docker exec $(docker ps -qf name=postgres) psql -U aliecs -d aliecs -c "SELECT username, display_name, status FROM users ORDER BY id"'`
  （库名/用户名以 release-meta.env 的 POSTGRES_* 为准。）

## 2. 网站 SSO 上线（双轨）

- 合并 PR → release-deploy 自动部署（迁移 0024 随 migrate.sh 执行）。
- 验证：登录弹窗出现「使用统一登录（SSO）」→ 跳 auth.hydwang.xyz →
  ssotest 登录 → 回站已登录、权限正确。
  ⚠️ ssotest 需先在网站 users 表建同名账号并分角色，否则回调 403
  （account not provisioned，属预期的默认拒绝）。
- 回退：任何问题改用旧密码登录（双轨并存）；彻底回退 =
  infra `sops set secrets/aliecs.enc.env '["OIDC_ENABLED"]' '"false"'`
  → push → aliecs render → 重跑 release-deploy（或改容器 env 重启 backend-api）。

## 3. Gokapi 切换（webdock1）

- Gokapi 后台 OIDC 配置：Provider URL `https://auth.hydwang.xyz/`、
  client_id `gokapi`、secret = `sops -d infra/secrets/sso-client-secrets.enc.env`
  的 `OIDC_PLAIN_GOKAPI`。
- 相关用户加入 lldap `files_users` 组。验证登录成功后再做第 4 节。

## 4. Authentik 退役（webdock1）

- `ssh webdock1 'systemctl stop authentik-ecs-tunnel && systemctl disable authentik-ecs-tunnel'`
  （unit 名以 `/etc/systemd/system` 实际为准）。
- `ssh webdock1 'cd /opt/webdock/deploy/authentik && docker compose down'`
  （**不删卷**：`/var/lib/authentik` 留档）。
- infra 清理：删 `config/webdock/webdock1-authentik-tunnel.env` 及
  render.sh webdock1 分支对应 install_file 行。
- ECS 侧 19000 端口的 permitlisten（如单独配置）可顺手收掉。

## 5. Immich / AdventureLog 接入（逐个，各自独立验证）

- Immich 管理后台 → OAuth：issuer `https://auth.hydwang.xyz`、client `immich`、
  secret = `OIDC_PLAIN_IMMICH`；回调地址若与
  `infra/server/authelia/configuration.yml` 的 redirect_uris 不一致，
  以应用后台为准改 config → push → aliecs render →
  `docker compose -f compose.sso.yml up -d --force-recreate authelia`。
- AdventureLog 同理（client `adventurelog`，secret = `OIDC_PLAIN_ADVENTURELOG`）。
- 用户按需加 lldap 组（family 等，组按需新建）。

## 6. 收口（观察 1-2 周后）

- 确认全员有 SSO 登录成功记录：audit_logs 查 `auth.oidc.login`。
- 旧密码登录仅留 admin break-glass；其余用户禁用密码前先通知。
- webdock 仓库删除 `deploy/authentik/`（先本地 pytest 再直推 main）。
- lldap/authelia 数据目录（/opt/sso/*-data）确认已入 restic 备份清单。

## 故障速查

| 症状 | 先查 |
|---|---|
| SSO 按钮 404 | backend-api env `OIDC_ENABLED` 是否 true（compose 默认 false） |
| 回调 400 invalid state | backend-api 是否重启过（state 在进程内存）；重试登录即可 |
| 回调 403 | 网站 users 表无同名账号或 status 非 active（默认拒绝，先建号） |
| 回调 502 | authelia 容器状态、backend→auth.hydwang.xyz 回环连通性 |
| Authelia 登录 401 | lldap 容器、`authelia` 绑定用户密码与 sso.enc.env 是否一致 |
