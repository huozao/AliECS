# Auth/RBAC 基础版说明

## 账号体系
- 用户保存在 `users` 表，密码仅保存 `password_hash`。
- 角色保存在 `roles`，权限保存在 `permissions`。
- 用户-角色、角色-权限通过关联表管理。

## 角色权限
- admin / manager / operator / viewer
- 关键权限：`admin.access`、`admin.users.manage`、`admin.roles.manage`、`admin.features.manage`、`personal.access` 等。

## 常见管理操作
1. 管理员登录 `http://localhost:8081`。
2. 在“用户管理”新增用户、禁用用户、重置密码。
3. 在“角色与权限”查看角色与权限。
4. 在“功能入口管理”新增或修改功能入口（active/reserved）。

## 新增用户
- 通过 `POST /v1/admin/users`。
- 再通过 `PUT /v1/admin/users/{user_id}/roles` 分配角色。

## 分配权限
- 通过 `PUT /v1/admin/roles/{role_id}/permissions`。

## 新增功能入口
- 通过 `POST /v1/admin/features`。
- 无真实链接可设置 `status=reserved`。

## 本地验证
```bash
docker compose -f local/docker-compose.local.yml up --build
```
- public-web: `http://localhost:8080`
- admin-ui: `http://localhost:8081`
- backend health: `http://localhost:8000/healthz`

## ECS 前检查
- 必填环境变量：
  - `AUTH_TOKEN_SECRET`
  - `AUTH_TOKEN_TTL_SECONDS`
  - `ADMIN_BOOTSTRAP_USERNAME`
  - `ADMIN_BOOTSTRAP_PASSWORD`
  - `ADMIN_BOOTSTRAP_DISPLAY_NAME`
- 生产必须修改默认管理员密码与 `AUTH_TOKEN_SECRET`。
