# 线上运行路由与入口映射（必须遵循）

> 目的：避免后续维护者或 Codex 因“路径前缀不一致”误判服务故障。

## 1. 当前生产约定

- 公网根地址：`https://hydwang.xyz`
- API 对外前缀：`/api`
- 后端容器内原始路由：`/healthz`、`/readyz`、`/v1/*`

因此，公网访问后端时应使用：

- `GET /api/healthz`
- `GET /api/readyz`
- `GET /api/v1/ping`
- `POST /api/v1/auth/login`
- `GET /api/v1/auth/me`

## 2. 页面入口映射

- public-web：`https://hydwang.xyz/`
- admin-ui：`https://hydwang.xyz/admin/`
- couple：`https://hydwang.xyz/couple/`

## 3. 常见误区

1. 直接请求 `https://hydwang.xyz/v1/auth/login`（缺少 `/api`）会导致 404/503/网关错误。
2. 仅验证页面能打开，不验证 `/api/*` 接口可用。
3. 把本地直连路径（如 `http://localhost:8000/v1/*`）当成公网路径。

## 4. 发布后最小验证（建议复制执行）

```bash
curl -fsS https://hydwang.xyz/api/healthz
curl -fsS https://hydwang.xyz/api/readyz
curl -fsS https://hydwang.xyz/api/v1/ping
```

如需验证登录，请仅使用测试账号，不要在日志或提交中打印密码。

