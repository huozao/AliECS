# 登录故障排查（基于最近版本）

日期：2026-05-12

## 结论（优先级从高到低）

1. **最可能是生产环境未正确注入 `AUTH_TOKEN_SECRET`（或仍为默认值）导致登录 500。**
   - 后端在 `ENV=prod` 时，如果 `AUTH_TOKEN_SECRET` 仍是默认值 `change-this-in-production`，会直接抛 500。
   - 登录接口在签发 token 时依赖该配置，因此会表现为“页面可打开，但提交登录失败”。

2. **次高概率是网关/反向代理没有把 `/api/*` 正确转发到后端。**
   - 当前前端（public-web/admin-ui）都按 `/api` 前缀请求后端。
   - 若 Nginx/ECS 外层路由配置丢失 `/api -> backend-api:8000`，会出现 404/502/503，用户主观感受也是“不能登录”。

3. **历史版本中确实有前端脚本导致登录按钮失效的问题，但已在后续提交修复。**
   - `933ccf1` 已修复 admin-ui 的重复 `const` 问题，避免点击登录无请求。

## 与“最近提交”关联的关键信号

- `e10a76e`：修复“登录 500”与 RBAC 缺失兼容；说明此前真实出现过登录失败。
- `933ccf1`：明确修复 admin-ui 登录相关 JS 问题。
- `8fdb1f0`：加强迁移与启动时序，降低因为数据库未就绪导致的登录异常。

这些提交说明：**最近确实在围绕“登录失败”连续补丁，部署后再失败更可能是环境/路由未与代码约定同步。**

## 快速验证步骤（线上）

> 在 ECS 主机执行。

1. 查看运行时变量是否注入：

```bash
cat /root/AliECS/deploy/ecs/runtime.env | sed -n '1,120p'
```

重点确认：
- `AUTH_TOKEN_SECRET` 非空，且不是 `change-this-in-production`
- `DATABASE_URL` 与 PostgreSQL 实际密码一致

2. 直接测后端健康：

```bash
curl -i http://127.0.0.1:8000/healthz
curl -i http://127.0.0.1:8000/readyz
```

3. 直接测公网 API 前缀：

```bash
curl -i https://hydwang.xyz/api/healthz
curl -i https://hydwang.xyz/api/v1/ping
```

4. 复现登录接口（不在命令历史保存明文密码）：

```bash
curl -i -X POST https://hydwang.xyz/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<你的测试密码>"}'
```

- 500：优先看 `AUTH_TOKEN_SECRET` / 数据库 / 迁移
- 404/502/503：优先看网关 `/api` 转发
- 401：账号密码或账号状态问题

## 建议修复顺序

1. 先核对 `release-meta.env` 与 `runtime.env` 是否一致，必要时重新执行：

```bash
bash deploy/ecs/deploy.sh <tag>
```

2. 若 API 前缀异常，修复反向代理后再测 `/api/healthz`。
3. 若仍 500，检查 backend-api 日志与数据库迁移状态。

## 回退方式

若线上持续无法登录，可按既有脚本回滚到上一版：

```bash
bash deploy/ecs/rollback.sh
```

回滚后立即重测：
- `/api/healthz`
- `/api/v1/ping`
- `/api/v1/auth/login`
