# Webdock OpenClaw Integration

`webdock` runs the ChatGPT browser relay on an Ubuntu laptop. ECS keeps OpenClaw and the lightweight bridge only.

## Runtime Topology

```text
WeChat -> OpenClaw on ECS -> /opt/openclaw-bridge -> Tailscale -> webdock laptop -> ChatGPT browser
```

Do not run the browser relay on the current small ECS instance. Chrome/Chromium can exhaust memory and affect SSH, Docker, and the main AliECS stack.

## ECS Bridge

The ECS bridge service is:

```text
openclaw-bridge.service
```

It reads optional runtime config from:

```text
/opt/openclaw-bridge/webdock.env
```

If the file is missing, the bridge keeps the local echo behavior:

```text
已收到你的微信消息：<message>
```

After the Ubuntu laptop webdock service is ready, create `/opt/openclaw-bridge/webdock.env` on ECS:

```env
WEB_DOCK_BASE_URL=http://<laptop_tailscale_ip>:18000/v1
WEB_DOCK_API_TOKEN=replace_with_long_random_api_token
WEB_DOCK_MODEL=browser-chatgpt
WEB_DOCK_TIMEOUT_SECONDS=180
WEB_DOCK_FALLBACK_MESSAGE=ChatGPT 浏览器暂不可用，请稍后再试。
```

Restart:

```bash
systemctl restart openclaw-bridge.service
```

Verify:

```bash
curl -s http://127.0.0.1:18080/v1/models
curl -s http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"browser-chatgpt","messages":[{"role":"user","content":"早"}],"stream":false}'
```

## Failure Behavior

If webdock is offline, busy, timed out, or not logged in, the bridge returns:

```text
ChatGPT 浏览器暂不可用，请稍后再试。
```

This avoids blocking OpenClaw or destabilizing ECS.
