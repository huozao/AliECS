# /health/ 添加新微信验证记录（2026-06-14）

## 已完成代码侧

- backend 新增 `/v1/ops/wechat/login-qr`，需要管理员权限。
- 端点支持两种二维码来源：
  - `OPENCLAW_WECHAT_LOGIN_QR_URL`：代理 OpenClaw/旁路服务返回的 `{qr_image_base64|qr_url, expires_at}`。
  - `OPENCLAW_WECHAT_LOGIN_QR_FILE`：读取二维码图片、data URL 或二维码 URL 文件。
- `/health/` 已新增「功能区」和「添加新微信」弹窗，支持刷新二维码。

## 调研结论

ECS 上 OpenClaw 文档显示 Weixin 登录方式是交互式 CLI：

```bash
openclaw channels login --channel openclaw-weixin
```

文档未说明稳定的 HTTP 取码 API。本期实现保留了 URL/文件两种接入口；如果 OpenClaw 后续提供稳定 API，只需配置 `OPENCLAW_WECHAT_LOGIN_QR_URL`。

## 已跑离线验证

```powershell
$env:PYTHONPATH='.'; pytest tests/test_wechat_login_qr.py -v
```

结果：`2 passed`。

## ECS 只读验证

```bash
ssh aliecs "docker inspect ecs-backend-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -E 'OPENCLAW_WECHAT_LOGIN_QR|OPENCLAW.*QR|WECHAT.*QR' | sed -E 's/=.*/=__SET__/g'"
```

结果：无输出，当前 backend 容器未配置二维码 URL/文件来源。

## ⚠️ 人工/ops 步骤

1. 在 OpenClaw 主机运行：

```bash
openclaw channels login --channel openclaw-weixin
```

2. 若只能在 CLI 显示二维码：人工扫码完成登录。
3. 若能把二维码图片/URL 落盘到 backend 可读路径：配置 `OPENCLAW_WECHAT_LOGIN_QR_FILE=/path/to/qr.png` 或 URL 文本文件。
4. 若另建旁路取码服务：配置 `OPENCLAW_WECHAT_LOGIN_QR_URL=http://.../wechat/login-qr`，返回 JSON：

```json
{"qr_image_base64":"data:image/png;base64,...","expires_at":"2026-06-14T12:00:00Z"}
```

5. 重新部署 backend 后，管理员打开 `/health/`，点击「添加新微信」，确认二维码可显示并用手机扫码。
