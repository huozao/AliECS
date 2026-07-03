# 统一账号系统（Authelia + lldap）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 aliecs 上部署 Authelia+lldap 轻量 IdP，网站 backend-api 接入 OIDC 登录（保留现有 HMAC 会话 token 与 RBAC），为 Gokapi/Immich/AdventureLog 统一登录铺路，退役 webdock1 Authentik。

**Architecture:** lldap（用户唯一事实源，<10MB）+ Authelia（OIDC 提供方，~30-50MB）两容器跑 aliecs，配置进 infra 仓库、密钥走 SOPS。backend-api 新增 OIDC 授权码+PKCE 登录路由，登录成功后按 username 匹配/绑定本地 users 记录并换发现有 HMAC token——前端会话逻辑零改动，RBAC 不动。

**Tech Stack:** Authelia 4.39（pin）、lldap v0.6（pin）、FastAPI + stdlib urllib（不新增 pip 依赖）、SOPS+age、PostgreSQL 幂等迁移。

**Spec:** `docs/superpowers/specs/2026-07-03-unified-account-system-design.md`

## Global Constraints

- aliecs 仅 2G 内存：两容器加 `mem_limit`（authelia 256m / lldap 128m），部署前后 `free -m` 对比，增量 >150MB 即叫停。
- backend-api **不新增 pip 依赖**：HTTP 用 stdlib `urllib.request`，不装 httpx/authlib/PyJWT。
- 身份校验走 **userinfo 端点**（server-to-server TLS 直连 issuer），不在本地验 id_token 签名（避免新增 crypto 依赖）。
- OIDC state/PKCE verifier 存进程内 dict——**前提是单 uvicorn worker**（现状如此），代码注释必须写明扩 worker 前要改存储。
- 首次绑定键 = `username`（lldap 的 uid 与网站 users.username 一致建号）；绑定后只认 `oidc_sub`。users 表无 email 列，**不用 email 匹配**。
- lldap web UI 只绑 `127.0.0.1:17170`，管理经 `ssh aliecs -L 17170:127.0.0.1:17170`，不暴露公网。
- AliECS 仓库改动走 PR（分支 `feature/unified-account-system` 已建）；infra 仓库直推 main（无 CI）；镜像 tag 锁定不追 latest。
- LDAP base DN 固定 `dc=hydwang,dc=xyz`。
- 全部新密钥进 SOPS（`infra/secrets/`），明文不落 git；RP 明文 client secret 存 devbox-only 的独立 enc 文件。
- UI 文案中文；不重排既有代码格式。

---

## Phase 1 — infra 仓库：IdP 栈

### Task 1: SSO 栈定义（compose + Authelia 配置 + render + sops 规则 + nginx）

**Files:**
- Create: `infra/server/compose.sso.yml`
- Create: `infra/server/authelia/configuration.yml`
- Modify: `infra/scripts/render.sh`（aliecs 分支加 2 行）
- Modify: `infra/.sops.yaml`（新 creation_rule）
- Modify: `infra/server/nginx/authentik-auth-http.conf`（上游 19000→9091）

**Interfaces:**
- Produces: Authelia OIDC issuer `https://auth.hydwang.xyz`；4 个 client_id：`website` / `gokapi` / `immich` / `adventurelog`；website 回调 `https://hydwang.xyz/api/v1/auth/oidc/callback`（Task 4 依赖这些值，一字不差）。
- Consumes: `/opt/sso/.env`（Task 2 渲染）、`/opt/sso/oidc.rsa.pem`（Task 2 生成）。

- [ ] **Step 1: 写 `infra/server/compose.sso.yml`**

```yaml
# SSO 栈 —— Authelia(OIDC IdP) + lldap(用户库)，跑在 aliecs。
# env 渲染产物: /opt/sso/.env       ← secrets/sso.enc.env (render.sh)
# OIDC 签名私钥: /opt/sso/oidc.rsa.pem ← secrets/sso-oidc-key.enc.pem (render.sh)
# lldap 管理 UI 只绑本机 17170，管理员用 ssh -L 访问，不走公网。
name: sso
services:
  lldap:
    image: lldap/lldap:v0.6-alpine   # pin 大版本；部署时核实最新 v0.6.x 补丁号
    container_name: sso-lldap
    restart: unless-stopped
    mem_limit: 128m
    environment:
      TZ: UTC
      LLDAP_LDAP_BASE_DN: dc=hydwang,dc=xyz
      LLDAP_HTTP_URL: http://127.0.0.1:17170
    env_file:
      - /opt/sso/.env                # LLDAP_JWT_SECRET / LLDAP_KEY_SEED / LLDAP_LDAP_USER_PASS
    ports:
      - "127.0.0.1:17170:17170"
    volumes:
      - /opt/sso/lldap-data:/data

  authelia:
    image: authelia/authelia:4.39    # pin 小版本系列；升级前读 release notes（OIDC 仍标 open beta）
    container_name: sso-authelia
    restart: unless-stopped
    mem_limit: 256m
    depends_on:
      - lldap
    environment:
      TZ: UTC
      X_AUTHELIA_CONFIG_FILTERS: template   # 让 configuration.yml 里 {{ env "..." }} 生效
    env_file:
      - /opt/sso/.env
    ports:
      - "127.0.0.1:9091:9091"
    volumes:
      - ./authelia/configuration.yml:/config/configuration.yml:ro
      - /opt/sso/oidc.rsa.pem:/config/oidc.rsa.pem:ro
      - /opt/sso/authelia-data:/data
```

- [ ] **Step 2: 写 `infra/server/authelia/configuration.yml`**

```yaml
# Authelia 4.39 —— hydwang.xyz 统一登录。
# 所有密钥经 template filter 从容器 env（/opt/sso/.env 渲染）注入，本文件不含明文。
theme: light
default_2fa_method: totp

server:
  address: tcp://0.0.0.0:9091

log:
  level: info

identity_validation:
  reset_password:
    jwt_secret: '{{ env "AUTHELIA_RESET_JWT_SECRET" }}'

totp:
  issuer: auth.hydwang.xyz

authentication_backend:
  ldap:
    address: ldap://lldap:3890
    implementation: lldap
    base_dn: dc=hydwang,dc=xyz
    user: uid=authelia,ou=people,dc=hydwang,dc=xyz
    password: '{{ env "AUTHELIA_LDAP_PASSWORD" }}'

access_control:
  default_policy: one_factor

session:
  secret: '{{ env "AUTHELIA_SESSION_SECRET" }}'
  cookies:
    - domain: hydwang.xyz
      authelia_url: https://auth.hydwang.xyz
      default_redirection_url: https://hydwang.xyz

regulation:
  max_retries: 5
  find_time: 2m
  ban_time: 10m

storage:
  encryption_key: '{{ env "AUTHELIA_STORAGE_ENCRYPTION_KEY" }}'
  local:
    path: /data/db.sqlite3

notifier:
  # SMTP 未配：重置邮件先落盘（改密以管理员在 lldap UI 操作为主）。配好 SMTP 后替换本段。
  filesystem:
    filename: /data/notification.txt

identity_providers:
  oidc:
    hmac_secret: '{{ env "AUTHELIA_OIDC_HMAC_SECRET" }}'
    jwks:
      - key_id: main
        algorithm: RS256
        use: sig
        key: {{ secret "/config/oidc.rsa.pem" | mindent 10 "|" | msquote }}
    clients:
      - client_id: website
        client_name: hydwang.xyz 网站
        client_secret: '{{ env "OIDC_DIGEST_WEBSITE" }}'
        authorization_policy: one_factor
        require_pkce: true
        pkce_challenge_method: S256
        token_endpoint_auth_method: client_secret_basic
        redirect_uris:
          - https://hydwang.xyz/api/v1/auth/oidc/callback
        scopes:
          - openid
          - profile
          - groups
      - client_id: gokapi
        client_name: AI 文件中转
        client_secret: '{{ env "OIDC_DIGEST_GOKAPI" }}'
        authorization_policy: one_factor
        redirect_uris:
          - https://files.hydwang.xyz/oauth-callback
        scopes:
          - openid
          - email
          - profile
          - groups
      - client_id: immich
        client_name: Immich
        client_secret: '{{ env "OIDC_DIGEST_IMMICH" }}'
        authorization_policy: one_factor
        redirect_uris:
          - https://immich.hydwang.xyz/auth/login
          - https://immich.hydwang.xyz/user-settings
          - app.immich:///oauth-callback
        scopes:
          - openid
          - email
          - profile
      - client_id: adventurelog
        client_name: AdventureLog
        client_secret: '{{ env "OIDC_DIGEST_ADVENTURELOG" }}'
        authorization_policy: one_factor
        redirect_uris:
          - https://adventure.hydwang.xyz/accounts/oidc/authelia/login/callback/
        scopes:
          - openid
          - email
          - profile
```

注：immich/adventurelog 的 redirect_uris 是按其当前版本文档写的默认值，Phase 3 接入各应用时若其后台显示不同回调地址，以应用后台为准改这里并 `docker compose up -d` 重载。

- [ ] **Step 3: `infra/scripts/render.sh` aliecs 分支追加渲染条目**

在 `render_env secrets/openclaw.enc.env /root/openclaw/.env` 一行之后插入：

```bash
    render_env secrets/sso.enc.env      /opt/sso/.env
    render_env secrets/sso-oidc-key.enc.pem /opt/sso/oidc.rsa.pem
```

- [ ] **Step 4: `infra/.sops.yaml` 给 sso 文件加规则**

在 aliecs-scoped 那条规则的 path_regex 里把 `sso` 加进分组（aliecs+devbox 可解）：

```yaml
  - path_regex: secrets[/\\](aliecs|server|openclaw|singbox|backup|sso|sso-oidc-key)\.enc\..*
    age: age148xu0xg8e28hrwgtklf4f6pct2ev5em0wcy3nvduac9v95h3jdjqpnap5m,age13rfrgkd2r35weennstq4et4fgwsrh3qntlmhtmgvgyqx5l5805cq25hvqj
```

并在其后新增 devbox-only 规则（RP 明文 client secret，仅 devbox 可解）：

```yaml
  # devbox-only: RP 明文 client secret 台账（不渲染到任何主机）
  - path_regex: secrets[/\\]sso-client-secrets\.enc\..*
    age: age13rfrgkd2r35weennstq4et4fgwsrh3qntlmhtmgvgyqx5l5805cq25hvqj
```

- [ ] **Step 5: 改 `infra/server/nginx/authentik-auth-http.conf` 上游**

`proxy_pass http://127.0.0.1:19000;` 改为 `proxy_pass http://127.0.0.1:9091;`，并在文件头加注释：

```nginx
# auth.hydwang.xyz —— 2026-07 起指向本机 Authelia(sso 栈, 127.0.0.1:9091)。
# 旧上游 19000 是 webdock1 Authentik 反向隧道，Authentik 已退役。
# tier2 mirror：本文件是仓库镜像，改动需手动应用到 /etc/nginx/conf.d/ 并 nginx -s reload。
```

- [ ] **Step 6: 本地校验 + 提交 infra**

```bash
cd infra
bash -n scripts/render.sh
git add server/compose.sso.yml server/authelia/configuration.yml scripts/render.sh .sops.yaml server/nginx/authentik-auth-http.conf
git commit -m "feat(sso): authelia+lldap stack on aliecs (compose/config/render/sops/nginx)"
```

预期：`bash -n` 无输出（语法通过）；commit 成功。**先不 push**（Task 2 生成密钥文件后一起推）。

### Task 2: 生成密钥、部署到 aliecs、初始化 lldap、验证

**Files:**
- Create: `infra/secrets/sso.enc.env`（SOPS 加密）
- Create: `infra/secrets/sso-oidc-key.enc.pem`（SOPS 加密）
- Create: `infra/secrets/sso-client-secrets.enc.env`（SOPS 加密，devbox-only）

**Interfaces:**
- Consumes: Task 1 的 compose/config/render。
- Produces: 运行中的 issuer `https://auth.hydwang.xyz`（discovery 200）；lldap 内 `authelia` 绑定账号与 `website_users` 组；website 的明文 client secret（Task 6 写进 aliecs.enc.env 用）。

- [ ] **Step 1: 在 aliecs 生成随机密钥与 RSA 私钥**（一次 ssh 完成，输出复制回 devbox 使用）

```bash
ssh aliecs 'for n in LLDAP_JWT_SECRET LLDAP_KEY_SEED LLDAP_LDAP_USER_PASS AUTHELIA_RESET_JWT_SECRET AUTHELIA_SESSION_SECRET AUTHELIA_STORAGE_ENCRYPTION_KEY AUTHELIA_OIDC_HMAC_SECRET AUTHELIA_LDAP_PASSWORD PLAIN_WEBSITE PLAIN_GOKAPI PLAIN_IMMICH PLAIN_ADVENTURELOG; do echo "$n=$(openssl rand -hex 32)"; done; openssl genrsa -out /tmp/oidc.rsa.pem 4096 && echo PEM_AT_/tmp/oidc.rsa.pem'
```

预期：12 行 `KEY=64hex` + PEM 生成提示。

- [ ] **Step 2: 在 aliecs 用 authelia 镜像把 4 个 PLAIN_* 算成 argon2 digest**

```bash
ssh aliecs 'for s in <PLAIN_WEBSITE值> <PLAIN_GOKAPI值> <PLAIN_IMMICH值> <PLAIN_ADVENTURELOG值>; do docker run --rm authelia/authelia:4.39 authelia crypto hash generate argon2 --password "$s" | sed "s/^Digest: //"; done'
```

预期：4 行 `$argon2id$...` digest（顺序对应 WEBSITE/GOKAPI/IMMICH/ADVENTURELOG）。

- [ ] **Step 3: devbox 上创建三个 SOPS 加密文件**

先写明文临时文件（Git Bash，`$env:SOPS_AGE_KEY_FILE` 已按既有约定配置）：

`/tmp/sso.plain.env`（8 个运行密钥 + 4 个 digest；digest 含 `$`，dotenv 原样存）：

```
LLDAP_JWT_SECRET=<step1值>
LLDAP_KEY_SEED=<step1值>
LLDAP_LDAP_USER_PASS=<step1值>
AUTHELIA_RESET_JWT_SECRET=<step1值>
AUTHELIA_SESSION_SECRET=<step1值>
AUTHELIA_STORAGE_ENCRYPTION_KEY=<step1值>
AUTHELIA_OIDC_HMAC_SECRET=<step1值>
AUTHELIA_LDAP_PASSWORD=<step1值>
OIDC_DIGEST_WEBSITE=<step2 digest>
OIDC_DIGEST_GOKAPI=<step2 digest>
OIDC_DIGEST_IMMICH=<step2 digest>
OIDC_DIGEST_ADVENTURELOG=<step2 digest>
```

`/tmp/sso-clients.plain.env`：

```
OIDC_PLAIN_WEBSITE=<step1 PLAIN_WEBSITE>
OIDC_PLAIN_GOKAPI=<step1 PLAIN_GOKAPI>
OIDC_PLAIN_IMMICH=<step1 PLAIN_IMMICH>
OIDC_PLAIN_ADVENTURELOG=<step1 PLAIN_ADVENTURELOG>
```

私钥从 aliecs 拷下：`scp aliecs:/tmp/oidc.rsa.pem /tmp/oidc.rsa.pem`

加密（`--filename-override` 让 .sops.yaml 规则按目标路径匹配）：

```bash
cd infra
sops -e --filename-override secrets/sso.enc.env /tmp/sso.plain.env > secrets/sso.enc.env
sops -e --filename-override secrets/sso-client-secrets.enc.env /tmp/sso-clients.plain.env > secrets/sso-client-secrets.enc.env
sops -e --input-type binary --output-type binary --filename-override secrets/sso-oidc-key.enc.pem /tmp/oidc.rsa.pem > secrets/sso-oidc-key.enc.pem
sops -d secrets/sso.enc.env | head -1        # 验证可解
rm /tmp/sso.plain.env /tmp/sso-clients.plain.env /tmp/oidc.rsa.pem
ssh aliecs 'rm /tmp/oidc.rsa.pem'
```

预期：`sops -d` 输出第一行 `LLDAP_JWT_SECRET=...`。

- [ ] **Step 4: 提交并推送 infra（origin + aliecs bare 仓）**

```bash
cd infra
git add secrets/sso.enc.env secrets/sso-client-secrets.enc.env secrets/sso-oidc-key.enc.pem
git commit -m "feat(sso): sso stack secrets (sops)"
git push origin main
git push device-aliecs main
```

- [ ] **Step 5: aliecs 渲染并起栈**

```bash
ssh aliecs 'cd /root/infra && git pull && ./scripts/render.sh aliecs && cd server && docker compose -f compose.sso.yml up -d && docker ps --format "{{.Names}} {{.Status}}" | grep sso'
```

预期：render 输出 `UPDATED: /opt/sso/.env`、`UPDATED: /opt/sso/oidc.rsa.pem`；`sso-authelia`/`sso-lldap` 均 Up。若 authelia 反复重启：`docker logs sso-authelia` 看配置校验错误（它启动时自校验 config）。

- [ ] **Step 6: 初始化 lldap（authelia 绑定账号 + 组 + 测试用户）**

devbox 开隧道 `ssh aliecs -L 17170:127.0.0.1:17170`，浏览器打开 `http://localhost:17170`，用 `admin` / `LLDAP_LDAP_USER_PASS` 登录：
1. 建用户 `authelia`（密码 = `AUTHELIA_LDAP_PASSWORD`），加入 `lldap_password_manager` 组。
2. 建组 `website_users`、`files_users`。
3. 建测试用户 `ssotest`（设密码），加入 `website_users`。

重启 authelia 使 LDAP 绑定生效：`ssh aliecs 'docker restart sso-authelia'`

- [ ] **Step 7: 验证**

```bash
ssh aliecs 'curl -fsS http://127.0.0.1:9091/.well-known/openid-configuration | head -c 200; echo; free -m'
curl -fsS https://auth.hydwang.xyz/.well-known/openid-configuration | head -c 200   # 走 nginx（需先手动应用 tier2 nginx conf 并 reload，见下）
```

nginx tier2 手动应用：

```bash
ssh aliecs 'cp /root/infra/server/nginx/authentik-auth-http.conf /etc/nginx/conf.d/authentik-auth-http.conf && nginx -t && nginx -s reload'
```

预期：两个 curl 都返回 JSON（含 `"issuer":"https://auth.hydwang.xyz"`）；浏览器打开 `https://auth.hydwang.xyz` 出 Authelia 登录页，`ssotest` 能登录；`free -m` 对比部署前增量 <150MB。
**容器内→issuer 回环预检**（Task 4 依赖）：`ssh aliecs 'docker exec $(docker ps -qf name=backend-api) python3 -c "import urllib.request;print(urllib.request.urlopen(\"https://auth.hydwang.xyz/.well-known/openid-configuration\",timeout=5).status)"'` 预期 200；若不通（hairpin 被挡），在 `deploy/ecs/compose.prod.yml` 的 backend-api 加 `extra_hosts: ["auth.hydwang.xyz:172.17.0.1"]` 并让 nginx 443 监听 docker0（记录进 Task 6 一并提交）。

---

## Phase 2 — AliECS 仓库：backend-api OIDC RP

### Task 3: users.oidc_sub 迁移

**Files:**
- Create: `db/migrations/0024_users_oidc_sub.sql`

**Interfaces:**
- Produces: `users.oidc_sub TEXT`（可空，部分唯一索引）。Task 4 的 SQL 依赖该列。

- [ ] **Step 1: 写迁移（幂等）**

```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_sub TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS users_oidc_sub_key ON users (oidc_sub) WHERE oidc_sub IS NOT NULL;
```

- [ ] **Step 2: 幂等性验证（本地有 docker 则跑，无则跳过，由部署时 migrate.sh 兜底）**

```bash
docker run --rm -d --name pgtest -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:16
sleep 5
docker exec -i pgtest psql -U postgres -c "CREATE TABLE users(id BIGSERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL);"
docker exec -i pgtest psql -U postgres < db/migrations/0024_users_oidc_sub.sql
docker exec -i pgtest psql -U postgres < db/migrations/0024_users_oidc_sub.sql   # 第二遍不报错=幂等
docker rm -f pgtest
```

预期：两遍均 `ALTER TABLE` / `CREATE INDEX`（或 NOTICE skip），零 ERROR。

- [ ] **Step 3: Commit**

```bash
git add db/migrations/0024_users_oidc_sub.sql
git commit -m "feat(auth): users.oidc_sub column for OIDC binding"
```

### Task 4: OIDC 登录路由（TDD）

**Files:**
- Create: `services/backend-api/app/routers/auth_oidc.py`
- Modify: `services/backend-api/app/main.py`（import + include_router，仿既有 6 个 router 的写法）
- Test: `tests/test_backend_oidc_login.py`

**Interfaces:**
- Consumes: `app.core` 的 `_audit, _conn, _encode_token, _token_ttl_seconds, _user_roles_permissions`（签名与 auth_admin.py 用法一致）；env `OIDC_ENABLED/OIDC_ISSUER/OIDC_CLIENT_ID/OIDC_CLIENT_SECRET/OIDC_REDIRECT_URI`。
- Produces: `GET /v1/auth/oidc/login`（302 到 Authelia）、`GET /v1/auth/oidc/callback?code&state`（HTMLResponse 写 localStorage 后跳 `/`）。模块级可 patch 点：`_http_get_json(url, headers)->dict`、`_http_post_form(url, data, headers)->dict`、`_conn`、`_audit`、`_pending_states`。

- [ ] **Step 1: 写失败测试 `tests/test_backend_oidc_login.py`**（完整文件）

```python
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"

OIDC_ENV = {
    "ENV": "dev",
    "AUTH_TOKEN_SECRET": "x" * 32,
    "OIDC_ENABLED": "true",
    "OIDC_ISSUER": "https://auth.hydwang.xyz",
    "OIDC_CLIENT_ID": "website",
    "OIDC_CLIENT_SECRET": "client-secret",
    "OIDC_REDIRECT_URI": "https://hydwang.xyz/api/v1/auth/oidc/callback",
}

DISCOVERY = {
    "authorization_endpoint": "https://auth.hydwang.xyz/api/oidc/authorization",
    "token_endpoint": "https://auth.hydwang.xyz/api/oidc/token",
    "userinfo_endpoint": "https://auth.hydwang.xyz/api/oidc/userinfo",
}

USERINFO = {"sub": "sub-123", "preferred_username": "alice", "groups": ["website_users"]}

# users 行序: id, username, display_name, status, is_admin, token_version
ALICE = (1, "alice", "Alice", "active", False, 1)


def load_oidc():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app.routers import auth_oidc

    return auth_oidc


class FakeCursor:
    def __init__(self, fetchone_script):
        self.fetchone_script = list(fetchone_script)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.fetchone_script.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


class OidcLoginTests(unittest.TestCase):
    def test_login_404_when_disabled(self):
        mod = load_oidc()
        with patch.dict(os.environ, {**OIDC_ENV, "OIDC_ENABLED": "false"}, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                mod.oidc_login()
        self.assertEqual(ctx.exception.status_code, 404)

    def test_login_redirects_with_pkce_and_state(self):
        mod = load_oidc()
        with patch.dict(os.environ, OIDC_ENV, clear=False):
            with patch.object(mod, "_http_get_json", return_value=DISCOVERY):
                response = mod.oidc_login()
        self.assertEqual(response.status_code, 302)
        location = response.headers["location"]
        self.assertTrue(location.startswith(DISCOVERY["authorization_endpoint"]))
        self.assertIn("code_challenge_method=S256", location)
        self.assertIn("client_id=website", location)
        self.assertEqual(len(mod._pending_states), 1)

    def test_callback_rejects_unknown_state(self):
        mod = load_oidc()
        with patch.dict(os.environ, OIDC_ENV, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                mod.oidc_callback(code="c", state="nope")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_callback_first_login_binds_by_username_and_sets_token(self):
        mod = load_oidc()
        cursor = FakeCursor([None, ALICE])  # sub 未命中 -> username 绑定 RETURNING 命中
        conn = FakeConn(cursor)
        mod._pending_states["s1"] = ("verifier-1", time.time())
        with patch.dict(os.environ, OIDC_ENV, clear=False):
            with patch.object(mod, "_http_get_json", side_effect=[DISCOVERY, USERINFO]) as get_json:
                with patch.object(mod, "_http_post_form", return_value={"access_token": "at-1"}) as post_form:
                    with patch.object(mod, "_conn", return_value=conn):
                        with patch.object(mod, "_audit") as audit:
                            with patch.object(mod, "_user_roles_permissions", return_value=([], [])):
                                response = mod.oidc_callback(code="code-1", state="s1")
        body = response.body.decode("utf-8")
        self.assertIn("aliecs_auth_token", body)
        self.assertTrue(conn.committed)
        self.assertEqual(post_form.call_args.args[1]["code_verifier"], "verifier-1")
        bind_sql, bind_params = cursor.executed[1]
        self.assertIn("SET oidc_sub", bind_sql)
        self.assertEqual(bind_params, ("sub-123", "alice"))
        audit.assert_called_once_with("alice", "auth.oidc.login")

    def test_callback_unknown_user_403(self):
        mod = load_oidc()
        cursor = FakeCursor([None, None])
        conn = FakeConn(cursor)
        mod._pending_states["s2"] = ("verifier-2", time.time())
        with patch.dict(os.environ, OIDC_ENV, clear=False):
            with patch.object(mod, "_http_get_json", side_effect=[DISCOVERY, USERINFO]):
                with patch.object(mod, "_http_post_form", return_value={"access_token": "at-2"}):
                    with patch.object(mod, "_conn", return_value=conn):
                        with self.assertRaises(HTTPException) as ctx:
                            mod.oidc_callback(code="code-2", state="s2")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_backend_oidc_login.py -v
```

预期：全部 FAIL/ERROR，`ModuleNotFoundError: app.routers.auth_oidc`（或 ImportError）。

- [ ] **Step 3: 写 `services/backend-api/app/routers/auth_oidc.py`**（完整文件）

```python
"""OIDC 登录域：对接 Authelia（授权码 + PKCE），成功后按 username 首绑/按 oidc_sub 复认，换发本站 HMAC 会话 token。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
import uuid

from contextlib import closing
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core import _audit, _conn, _encode_token, _token_ttl_seconds, _user_roles_permissions

router = APIRouter()

_STATE_TTL_SECONDS = 600
# state -> (code_verifier, created_at)。进程内存态：现网单 uvicorn worker 成立；
# 若将来扩多 worker/多实例，必须改成 DB/共享存储，否则回调会随机 400。
_pending_states: dict[str, tuple[str, float]] = {}
_discovery_cache: dict[str, dict[str, Any]] = {}


def _oidc_enabled() -> bool:
    return os.getenv("OIDC_ENABLED", "false").strip().lower() == "true"


def _oidc_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=500, detail=f"{name} not configured")
    return value


def _http_get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_post_form(url: str, data: dict[str, str], headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    merged = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    request = urllib.request.Request(url, data=body, headers=merged)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _discovery() -> dict[str, Any]:
    issuer = _oidc_env("OIDC_ISSUER").rstrip("/")
    cached = _discovery_cache.get(issuer)
    if cached:
        return cached
    doc = _http_get_json(f"{issuer}/.well-known/openid-configuration")
    _discovery_cache[issuer] = doc
    return doc


def _prune_states(now: float) -> None:
    expired = [key for key, (_, created) in _pending_states.items() if now - created > _STATE_TTL_SECONDS]
    for key in expired:
        _pending_states.pop(key, None)


@router.get("/v1/auth/oidc/login")
def oidc_login() -> RedirectResponse:
    if not _oidc_enabled():
        raise HTTPException(status_code=404, detail="oidc disabled")
    now = time.time()
    _prune_states(now)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    _pending_states[state] = (verifier, now)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    params = {
        "response_type": "code",
        "client_id": _oidc_env("OIDC_CLIENT_ID"),
        "redirect_uri": _oidc_env("OIDC_REDIRECT_URI"),
        "scope": "openid profile groups",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(
        f"{_discovery()['authorization_endpoint']}?{urllib.parse.urlencode(params)}", status_code=302
    )


@router.get("/v1/auth/oidc/callback")
def oidc_callback(code: str = "", state: str = "") -> HTMLResponse:
    if not _oidc_enabled():
        raise HTTPException(status_code=404, detail="oidc disabled")
    entry = _pending_states.pop(state, None) if state else None
    if not code or entry is None or time.time() - entry[1] > _STATE_TTL_SECONDS:
        raise HTTPException(status_code=400, detail="invalid state")

    doc = _discovery()
    client_id = _oidc_env("OIDC_CLIENT_ID")
    basic = base64.b64encode(f"{client_id}:{_oidc_env('OIDC_CLIENT_SECRET')}".encode("utf-8")).decode("ascii")
    token_doc = _http_post_form(
        doc["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _oidc_env("OIDC_REDIRECT_URI"),
            "code_verifier": entry[0],
        },
        headers={"Authorization": f"Basic {basic}"},
    )
    access_token = str(token_doc.get("access_token", ""))
    if not access_token:
        raise HTTPException(status_code=502, detail="token exchange failed")

    userinfo = _http_get_json(doc["userinfo_endpoint"], headers={"Authorization": f"Bearer {access_token}"})
    sub = str(userinfo.get("sub", "")).strip()
    preferred = str(userinfo.get("preferred_username", "")).strip()
    if not sub:
        raise HTTPException(status_code=502, detail="userinfo missing sub")

    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, display_name, status, is_admin, token_version FROM users WHERE oidc_sub = %s",
                (sub,),
            )
            row = cur.fetchone()
            if row is None and preferred:
                # 首次登录：按 username 绑定（lldap uid 与网站 username 一致建号）；绑定后只认 oidc_sub。
                cur.execute(
                    """
                    UPDATE users SET oidc_sub = %s, updated_at = NOW()
                    WHERE username = %s AND oidc_sub IS NULL
                    RETURNING id, username, display_name, status, is_admin, token_version
                    """,
                    (sub, preferred),
                )
                row = cur.fetchone()
            if row is None or row[3] != "active":
                conn.rollback()
                raise HTTPException(status_code=403, detail="account not provisioned")

            user_id = int(row[0])
            roles, permissions = _user_roles_permissions(user_id, bool(row[4]))
            now_ts = int(time.time())
            payload = {
                "sub": row[1],
                "uid": user_id,
                "display_name": row[2],
                "roles": roles,
                "permissions": permissions,
                "tv": int(row[5]),
                "jti": uuid.uuid4().hex,
                "iat": now_ts,
                "exp": now_ts + _token_ttl_seconds(),
            }
            cur.execute("UPDATE users SET last_login_at = NOW(), updated_at = NOW() WHERE id = %s", (user_id,))
        conn.commit()

    _audit(row[1], "auth.oidc.login")
    token_js = json.dumps(_encode_token(payload))
    html = (
        '<!doctype html><meta charset="utf-8"><title>登录成功</title>'
        "<script>var token=" + token_js + ";"
        '["aliecs_auth_token","portal_token","admin_token"].forEach(function(key){localStorage.setItem(key,token);});'
        'location.replace("/");</script>登录成功，正在跳转……'
    )
    return HTMLResponse(html)
```

- [ ] **Step 4: `services/backend-api/app/main.py` 装配**

import 区按字母序加一行、include 区加一行（与既有 6 个 router 写法一致）：

```python
from app.routers.auth_oidc import router as auth_oidc_router
```

```python
app.include_router(auth_oidc_router)
```

- [ ] **Step 5: 跑测试到全绿**

```bash
python -m pytest tests/test_backend_oidc_login.py -v
```

预期：5 passed。

- [ ] **Step 6: 全量回归**

```bash
python -m pytest tests/ -q
```

预期：全部 pass（无既有测试被破坏）。

- [ ] **Step 7: Commit**

```bash
git add services/backend-api/app/routers/auth_oidc.py services/backend-api/app/main.py tests/test_backend_oidc_login.py
git commit -m "feat(auth): OIDC login via Authelia (code+PKCE, username first-bind, HMAC token reissue)"
```

### Task 5: public-web 登录弹窗加「统一登录」按钮

**Files:**
- Modify: `services/public-web/index.html`（登录 modal + JS）

**Interfaces:**
- Consumes: Task 4 的 `GET /v1/auth/oidc/login`（经 `API_BASE` 前缀）。

- [ ] **Step 1: modal 里密码输入行后加按钮**（`<input id="modalPassword" .../>` 之后）

```html
<button id="modalSso" type="button" class="secondary">使用统一登录（SSO）</button>
```

（class 与 modal 内既有次要按钮保持一致；若无 secondary 类则复用现有按钮样式类。）

- [ ] **Step 2: JS 绑定**（`modalPassword.onkeydown=...` 附近加一行）

```javascript
document.getElementById('modalSso').onclick=()=>{location.href=`${API_BASE}/v1/auth/oidc/login`;};
```

注：本地开发（8080→localhost:8000 跨源）下回调页写的 localStorage 不在 8080 源上，SSO 按钮仅生产（同源 `/api`）有效——按钮旁不用提示，生产才是真实用户路径。

- [ ] **Step 3: 回归前端契约测试 + Commit**

```bash
python -m pytest tests/ -q -k frontend
git add services/public-web/index.html
git commit -m "feat(web): SSO login button on portal login modal"
```

预期：frontend 相关测试全 pass（若某契约测试断言 modal 结构，按其断言微调）。

### Task 6: 部署链路 env 贯通（deploy.sh / compose.prod / example / infra secrets）

**Files:**
- Modify: `deploy/ecs/deploy.sh`（heredoc `ENV` 块尾部加 5 行）
- Modify: `deploy/ecs/compose.prod.yml`（backend-api environment 加 5 行）
- Modify: `deploy/ecs/release-meta.env.example`、`deploy/ecs/runtime.env.example`（各加 5 行示例）
- Modify: `infra/secrets/aliecs.enc.env`（sops set 5 个键）

**Interfaces:**
- Consumes: Task 2 的 `OIDC_PLAIN_WEBSITE`（明文 client secret，从 `sops -d infra/secrets/sso-client-secrets.enc.env` 读取）。
- Produces: backend-api 容器内 `OIDC_*` 5 个 env。

- [ ] **Step 1: `deploy/ecs/deploy.sh` heredoc 尾部（`ENV` 结束符前）追加**

```bash
OIDC_ENABLED=${OIDC_ENABLED:-false}
OIDC_ISSUER=${OIDC_ISSUER:-}
OIDC_CLIENT_ID=${OIDC_CLIENT_ID:-}
OIDC_CLIENT_SECRET=${OIDC_CLIENT_SECRET:-}
OIDC_REDIRECT_URI=${OIDC_REDIRECT_URI:-}
```

（带 `:-` 默认值：release-meta.env 未配时部署不炸、功能关闭。）

- [ ] **Step 2: `deploy/ecs/compose.prod.yml` backend-api `environment:` 追加**

```yaml
      OIDC_ENABLED: ${OIDC_ENABLED:-false}
      OIDC_ISSUER: ${OIDC_ISSUER:-}
      OIDC_CLIENT_ID: ${OIDC_CLIENT_ID:-}
      OIDC_CLIENT_SECRET: ${OIDC_CLIENT_SECRET:-}
      OIDC_REDIRECT_URI: ${OIDC_REDIRECT_URI:-}
```

（若 Task 2 Step 7 的回环预检失败，同时在此加 `extra_hosts: ["auth.hydwang.xyz:172.17.0.1"]`。）

- [ ] **Step 3: 两个 example 文件追加同名 5 行**（值为示例）

```
OIDC_ENABLED=false
OIDC_ISSUER=https://auth.hydwang.xyz
OIDC_CLIENT_ID=website
OIDC_CLIENT_SECRET=change-me
OIDC_REDIRECT_URI=https://hydwang.xyz/api/v1/auth/oidc/callback
```

- [ ] **Step 4: infra 侧写真实值（devbox）**

```bash
cd infra
SECRET=$(sops -d secrets/sso-client-secrets.enc.env | grep '^OIDC_PLAIN_WEBSITE=' | cut -d= -f2)
sops set secrets/aliecs.enc.env '["OIDC_ENABLED"]' '"true"'
sops set secrets/aliecs.enc.env '["OIDC_ISSUER"]' '"https://auth.hydwang.xyz"'
sops set secrets/aliecs.enc.env '["OIDC_CLIENT_ID"]' '"website"'
sops set secrets/aliecs.enc.env '["OIDC_CLIENT_SECRET"]' "\"$SECRET\""
sops set secrets/aliecs.enc.env '["OIDC_REDIRECT_URI"]' '"https://hydwang.xyz/api/v1/auth/oidc/callback"'
git add secrets/aliecs.enc.env && git commit -m "feat(sso): website OIDC client env for backend-api" && git push origin main && git push device-aliecs main
ssh aliecs 'cd /root/infra && git pull && ./scripts/render.sh aliecs'
```

预期：render 输出 `UPDATED: /root/AliECS/deploy/ecs/release-meta.env`（下次 release-deploy 自动带上）。

- [ ] **Step 5: 回归 + Commit（AliECS）**

```bash
python -m pytest tests/ -q
git add deploy/ecs/deploy.sh deploy/ecs/compose.prod.yml deploy/ecs/release-meta.env.example deploy/ecs/runtime.env.example
git commit -m "feat(auth): wire OIDC env through deploy pipeline (default off)"
```

---

## Phase 3 — 收尾：PR、上线验证、cutover runbook

### Task 7: cutover runbook + webdock 废弃标记 + PR

**Files:**
- Create: `docs/sso-cutover-runbook.md`（AliECS）
- Modify: `webdock/docs/authentik.md`（头部加废弃注记）

**Interfaces:**
- Consumes: Phase 1/2 全部产出。

- [ ] **Step 1: 写 `docs/sso-cutover-runbook.md`**（内容含以下段落，值均为本计划已定值）

```markdown
# SSO 切换 runbook（Authelia 统一登录）

## 1. 网站用户建号（lldap）
- `ssh aliecs -L 17170:127.0.0.1:17170` → http://localhost:17170（admin / LLDAP_LDAP_USER_PASS）。
- 对照 users 表逐个建号：lldap 用户名 = 网站 username（一字不差，绑定键）；加入 `website_users` 组；设初始密码线下分发。
- 查现网用户清单：`ssh aliecs 'docker exec $(docker ps -qf name=postgres) psql -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT username, display_name, status FROM users ORDER BY id"'`

## 2. 网站 SSO 上线（双轨）
- 合并 PR → release-deploy 自动部署（迁移 0024 随 migrate.sh 执行）。
- 验证：登录弹窗出现「使用统一登录（SSO）」→ 跳 auth.hydwang.xyz → ssotest 登录 → 回站已登录、权限正确（ssotest 需先在 users 表建同名账号并分角色）。
- 回退：任何问题改用旧密码登录（双轨并存）；彻底回退 = infra 把 OIDC_ENABLED 设 false + render + 重部署。

## 3. Gokapi 切换（webdock1）
- Gokapi 后台 OIDC 配置：Provider URL https://auth.hydwang.xyz、client_id gokapi、secret = sops -d infra/secrets/sso-client-secrets.enc.env 的 OIDC_PLAIN_GOKAPI。
- 用户加入 lldap `files_users` 组。验证登录后再做下一步。

## 4. Authentik 退役（webdock1）
- systemctl stop/disable authentik-ecs-tunnel（unit 名见 webdock1 /etc/systemd/system）。
- cd /opt/webdock/deploy/authentik && docker compose down（不删卷：/var/lib/authentik 留档）。
- infra: config/webdock/webdock1-authentik-tunnel.env 与 render.sh webdock1 分支对应行删除。

## 5. Immich / AdventureLog 接入（逐个，各自独立验证）
- Immich 管理后台 OAuth：issuer https://auth.hydwang.xyz、client immich、secret=OIDC_PLAIN_IMMICH；回调若与 infra/server/authelia/configuration.yml 不符，以应用后台为准改 config 后 compose up -d。
- AdventureLog 同理（client adventurelog）。

## 6. 收口（观察 1-2 周后）
- 旧密码登录仅留 admin break-glass：给非 admin 用户的 password_hash 置为不可匹配值前，先确认全员 SSO 登录成功记录（audit_logs 查 auth.oidc.login）。
- webdock 仓库删除 deploy/authentik/（先跑本地 pytest 再直推）。
```

- [ ] **Step 2: `webdock/docs/authentik.md` 头部加注记**

```markdown
> **DEPRECATED (2026-07):** Authentik 由 aliecs 上的 Authelia+lldap 统一登录替代
> （见 AliECS `docs/superpowers/specs/2026-07-03-unified-account-system-design.md`）。
> 本部署单元待 SSO 收口后删除；退役步骤见 AliECS `docs/sso-cutover-runbook.md`。
```

- [ ] **Step 3: Commit + PR（AliECS）；webdock 直推前本地 pytest**

```bash
git add docs/sso-cutover-runbook.md
git commit -m "docs: SSO cutover runbook"
git push -u origin feature/unified-account-system
gh pr create --title "feat: unified account system — OIDC login via Authelia (backend RP + deploy wiring)" --body "见 docs/superpowers/specs/2026-07-03-unified-account-system-design.md"
cd ../webdock && python -m pytest -q && git add docs/authentik.md && git commit -m "docs: mark authentik deployment deprecated (replaced by aliecs authelia sso)" && git push origin main
```

预期：PR 创建成功；webdock pytest 全绿后直推 main。

- [ ] **Step 4: 合并后上线验证（等用户拍板合并）**

- PR 合并 → release-deploy 完成 → `curl -fsS https://hydwang.xyz/api/healthz`。
- 真机走 runbook 第 2 节验证 SSO 登录端到端。
- `ssh aliecs free -m` 复核内存水位。
```

## 执行顺序与依赖

Task 1 → 2（infra，可先行）；Task 3 → 4 → 5 → 6（AliECS，依赖 Task 2 的 issuer 在线才能生产验证，但代码与测试不依赖）；Task 7 收尾。Phase 3 的 runbook 第 1/3/4/5/6 节是**人工/后续会话操作**，不在本次代码交付内。
