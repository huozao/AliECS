# 环境变量矩阵（开发 / 部署 / 验证）

> 目标：让人和 AI 在修改配置时可快速判断“变量影响范围、缺失后果、验证方式”。

## 1. 线上验证（E2E）变量

| 变量名 | 作用 | 是否必填 | 缺失后果 |
|---|---|---|---|
| `APP_BASE_URL` | 项目部署根地址 | 建议 | 验证脚本可能使用错误域名 |
| `E2E_BASE_URL` | 端到端验证根地址（建议用于 API 基准） | 建议 | 端到端脚本无法统一入口 |
| `E2E_PUBLIC_URL` | 公网首页 | 建议 | 无法验证 public-web 入口 |
| `E2E_ADMIN_URL` | 管理后台入口 | 建议 | 无法验证 admin-ui 入口 |
| `E2E_COUPLE_URL` | Couple 页面入口 | 建议 | 无法验证 couple 入口 |
| `E2E_LOGIN_USERNAME`/`E2E_LOGIN_EMAIL` | 测试账号标识 | 建议（二选一） | 无法执行登录 smoke |
| `E2E_LOGIN_PASSWORD` | 测试账号密码 | 建议 | 无法执行登录 smoke |
| `E2E_LOGIN_ROLE` | 预期角色校验 | 可选 | 无法做角色一致性断言 |

> 注意：生产路由有 `/api` 前缀时，建议 `E2E_BASE_URL` 设置为 `https://<domain>/api`。

## 2. 鉴权与管理员引导

| 变量名 | 作用 | 是否必填 | 缺失后果 |
|---|---|---|---|
| `AUTH_TOKEN_SECRET` | JWT 签名密钥 | 生产必填 | 鉴权安全性不足或无法按预期工作 |
| `AUTH_TOKEN_TTL_SECONDS` | token 过期时间 | 建议 | 使用默认值，可能与预期不一致 |
| `ADMIN_BOOTSTRAP_USERNAME` | 初始化管理员用户名 | 建议 | 使用默认值，增加误用风险 |
| `ADMIN_BOOTSTRAP_PASSWORD` | 初始化管理员密码 | 生产必填 | 默认密码风险极高 |
| `ADMIN_BOOTSTRAP_DISPLAY_NAME` | 初始化管理员展示名 | 可选 | 使用默认值 |

## 3. Couple 功能入口控制

| 变量名 | 作用 | 是否必填 | 缺失后果 |
|---|---|---|---|
| `COUPLE_FEATURE_ENABLED` | Couple 入口总开关 | 建议 | 入口判定不符合预期 |
| `COUPLE_ROUTE` | Couple 路径 | 建议 | 前后端入口不一致 |
| `COUPLE_ALLOWED_USERS` | 用户白名单 | 建议 | 授权边界不明确 |
| `COUPLE_ALLOWED_EMAILS` | 邮箱白名单 | 可选 | 无法按邮箱维度限制 |

## 4. 维护要求

1. 新增变量时必须同步更新本文档与示例配置文件。
2. 不得提交真实生产密钥、密码、token、私钥。
3. 变量缺失时，代码应给出明确报错或降级行为，避免静默失败。

