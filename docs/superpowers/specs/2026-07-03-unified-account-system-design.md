# 统一账号系统设计（Authelia + lldap）

日期：2026-07-03
状态：已获用户批准

## 背景与问题

现状为 4 套账号孤岛：

| 系统 | 位置 | 实现 | 用途 |
|---|---|---|---|
| 网站账号 | aliecs backend-api | 自建 users 表 + 自制 HMAC token + RBAC | hydwang.xyz 业务查询 |
| Authentik | webdock1（隧道→ECS nginx） | 全栈 server+worker+postgres+redis | 仅 Gokapi（AI 文件中转）OIDC |
| AdventureLog | webdock1 | 自带账号 | 地图相册 |
| Immich | webdock1 | 自带账号 | 照片 |

痛点（用户确认）：账号孤岛太多；网站自建认证太弱（非标准 token、无 MFA、无自助密码重置）。

约束（用户确认）：

- 登录必须高可用 → IdP 必须在 aliecs，不能依赖有断电风险的 webdock1/2。
- 不加预算 → 塞进 aliecs 现有 2G 内存（已长期超卖），排除 Authentik（~1GB+）。
- 密码登录必须保留 → 排除 passkey-only 方案（PocketID）。

## 选型结论

**Authelia + lldap**，两个轻量容器跑在 aliecs：

- Authelia（Go）：OIDC 提供方 + 密码/TOTP/passkey MFA，~30-50MB。
  4.39 为稳定版（4.39.20，2026-05）；OIDC 提供方功能官方标 open beta，
  但核心流程被大量生产使用 → 应对：锁版本、升级前读 release notes。
- lldap（Rust）：用户唯一事实源，自带 web 管理 UI（建号/改密/分组），
  配 SMTP 后用户自助收密码重置链接，默认 SQLite，实测内存 <10MB。

落选：Kanidm（管理偏 CLI，不解决管理体验痛点）、Casdoor（~150-250MB 偏挤 +
历史 CVE 较多，不适合当全站大门）、Authentik（内存装不下）、
Keycloak（Java 更重）、PocketID（仅 passkey）。

原则：**IdP 只管身份（谁登录了、属于哪个组），网站 RBAC 角色权限表留在
backend-api 不动**。业务权限跟业务代码走，将来换 IdP 不伤业务。

## 架构

```
用户浏览器
  → auth.hydwang.xyz (aliecs nginx, TLS)
      → 127.0.0.1:9091  Authelia 容器（OIDC 提供方 + 密码/MFA）
          → lldap 容器 :3890（用户唯一事实源）
              lldap web 管理 UI :17170（只绑本机，管理员经 ssh -L 隧道访问，不暴露公网）

各应用 = OIDC RP：
  hydwang.xyz 网站 (backend-api) ─┐
  Gokapi 文件中转                  ├─ OIDC → Authelia
  Immich 相册                      │
  AdventureLog                     ┘
```

- compose 与配置进 **infra 仓库**（平台层，与 nginx/bridge 同层），
  密钥走既有 SOPS+render 流程。内存增量 ≈ 60MB。
- `auth.hydwang.xyz` 从「隧道→webdock1 Authentik」改指本机 Authelia；
  **webdock1 Authentik 退役**（数据备份留档）。
- 存储：两者均 SQLite（单节点足够），纳入现有 restic 备份。
  不引入 Redis：Authelia 单实例用内存会话，重启后 SSO 会话重登（可接受），
  已换发的网站 token 不受影响。

## 账号模型

| 职责 | 归属 |
|---|---|
| 用户身份：用户名/邮箱/密码/分组 | lldap（唯一事实源，web UI 管理） |
| 登录/MFA/签发 OIDC token | Authelia |
| 网站业务角色与权限（RBAC 表、功能入口） | backend-api 不动：OIDC sub/username 匹配本地 users 记录（users 表无 email 列，lldap 用户名与网站 username 一致建号） |
| 各应用准入 | lldap 组（website_users / files_users / family…），Authelia 按组限制 |

- backend-api users 表**保留但卸掉密码职责**（password_hash 停用）；
  OIDC 登录成功后仍发放现有 HMAC 会话 token → 前端与现有接口零改动。
- **逃生舱**：admin 一个本地密码账号保留（break-glass），
  Authelia 全挂时管理员仍能进 admin-ui。

## 对接改造点（按仓库）

| 仓库/位置 | 改动 |
|---|---|
| infra | authelia+lldap compose 与配置模板；4 个 RP 的 client secret 进 SOPS；nginx `auth.hydwang.xyz` 改指本机 9091；lldap UI 挂子域/子路径 |
| AliECS backend-api | `GET /v1/auth/oidc/login`（跳转）+ `GET /v1/auth/oidc/callback`（换 token→匹配 users→发 HMAC token）；users 表加 `oidc_sub` 列（幂等迁移+唯一约束）；登录页加「统一登录」按钮 |
| Gokapi | 配置：Provider URL 换成 Authelia（无代码改动） |
| Immich / AdventureLog | 各自后台填 OIDC 配置（无代码改动） |
| webdock 仓库 | `deploy/authentik/` 标记废弃（退役后删除） |

不动：admin-ui 角色权限管理、现有前端会话逻辑、MCP OAuth AS。

## 迁移步骤（每步可独立验证、可回退）

1. **起服务**：aliecs 部署 lldap+Authelia，不动 nginx 主路由，
   用测试路径验证 OIDC 发现端点。
2. **建账号**：现有网站用户在 lldap 建号+分组；邮箱齐的发重置链接，
   不齐的管理员设初始密码。
3. **网站双轨**：backend-api 上线 OIDC 登录，旧密码登录并存。
4. **切 Gokapi**：`auth.hydwang.xyz` 切指本机 Authelia，Gokapi 改
   Provider URL；验证后 webdock1 Authentik 停容器（数据留档）。
5. **接 Immich / AdventureLog**：逐个配置独立验证。
6. **收口**：观察 1-2 周后，旧密码登录只留 admin break-glass。

## 失败模式

| 场景 | 后果 | 兜底 |
|---|---|---|
| Authelia 挂 | 新登录不可用；已登录会话不受影响（token 独立 TTL 8h） | break-glass 本地密码；compose 自动重启 |
| Authelia 重启 | SSO 会话丢（内存会话） | 重登一次，可接受 |
| SMTP 未配/挂 | 自助密码重置不可用 | 管理员 lldap UI 手动改密 |
| OIDC beta 踩坑 | 某 RP 对接异常 | 锁版本；双轨期退回旧登录 |
| aliecs 内存压力 | 新增 ~60MB | 部署前后 `free -m` 对比，超预期叫停 |

## 测试

- 本地：`local/docker-compose.local.yml` 加 authelia+lldap，
  端到端跑「lldap 建号 → OIDC 登录 → backend 发 token → 权限正确」。
- CI：OIDC 回调逻辑单测（mock IdP、sub 匹配、未匹配用户拒绝）。
- 生产：每步迁移用测试账号实登验证。
  账号映射错误零容忍：`oidc_sub` 唯一约束，username 匹配仅限首次绑定。

## 安全要点

- 授权码流程 + PKCE；client secret 全部 SOPS 管理，不进明文。
- `oidc_sub` 为绑定权威标识；username 仅用于首次绑定匹配，绑定后不再作为查找键
  （防用户名重建/重用导致串号）。
- 未在 users 表中匹配到且无自动供应规则的 OIDC 用户，拒绝登录（默认拒绝）。
- lldap 管理 UI 仅 `lldap_admin` 组可用；对接服务用只读/密码管理账号，
  不给全量管理员权限。
