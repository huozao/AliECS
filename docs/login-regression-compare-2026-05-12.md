# 登录故障版本对比（十多个小时前版本 vs 最近版本）

日期：2026-05-12

## 对比基线

- 十多个小时前 GitHub 最后提交版本（约 2026-05-12 06:03 +0800）：`b94ffd1`
- 最近提交版本（约 2026-05-13 00:14 +0800）：`dda1faa`

本次对比命令：

```bash
git log --pretty=format:'%h %ad %s' --date=iso -n 20
git diff --stat b94ffd1 dda1faa
git diff b94ffd1 dda1faa -- services/backend-api/app/main.py
```

## 改动范围（b94ffd1 -> dda1faa）

核心改动集中在：
- `services/backend-api/app/main.py`
- `services/public-web/common/user-badge.js`
- `services/public-web/couple/index.html`
- 多个公共页面读取登录态逻辑
- 发布工作流 `release-deploy.yml`

## 影响登录的关键差异

### 1) 登录 500 的直接修复点出现在 `e10a76e`

`e10a76e` 在后端 `auth_login` 路径中做了两处容错：

1. `_user_roles_permissions` 增加 `try/except`：
   - 当 RBAC 关联表缺失（如 `user_roles` / `role_permissions`）或查询异常时，不再抛错中断登录；
   - 改为回退到空 roles/permissions，并按 `is_admin` 最小补齐。

2. `pwd_ctx.verify(...)` 增加异常容错：
   - 避免历史脏 hash/异常 hash 直接触发 500。

这说明：**在该修复之前，只要 RBAC 相关表没准备好（常见于“代码已更新但迁移未成功/未执行”），登录就会在查询角色权限时 500。**

### 2) 根因不是“最新版本引入登录故障”，而是“中间版本暴露了环境迁移不一致”

从提交链看：
- 先引入了更多依赖 RBAC/登录态展示的能力；
- 线上环境如果没有同步完成迁移，就会在登录后半段（查角色权限）报错；
- `e10a76e` 再用兼容策略兜底，避免 500。

因此你看到的“部署后不能登录”在时间线上更像：

- **触发条件**：上线了依赖 RBAC 的代码，但目标环境 RBAC 表/数据没到位；
- **表现**：`/v1/auth/login` 返回 500（不是 401）；
- **后续修复**：`e10a76e` 降级兼容后恢复可登录。

## 你现在该怎么快速确认

1. 在问题版本上直接打登录接口，看是否 500。
2. 进入数据库确认 RBAC 相关表是否存在，迁移是否跑完。
3. 检查部署日志里 `deploy/ecs/migrate.sh` 是否成功执行。
4. 若必须先恢复服务，可回退上一版或直接升级到包含 `e10a76e` 的版本。

## 一句话结论

`b94ffd1` 到 `dda1faa` 之间，**与“不能登录”最相关的是 `e10a76e` 这个“登录500修复提交”本身揭示出的事实：线上曾因 RBAC 缺失/异常导致登录流程在角色权限查询处崩溃**。根因在部署迁移一致性，而不是单纯前端按钮或账号密码错误。
