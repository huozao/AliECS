# WebDock OpenClaw Integration

`webdock` runs the ChatGPT browser relay on an Ubuntu laptop. ECS keeps OpenClaw and the lightweight OpenAI-compatible bridge only.

## Runtime Topology

```text
WeChat -> OpenClaw on ECS -> openclaw-bridge -> ECS localhost reverse tunnel -> webdock laptop -> ChatGPT browser
```

Do not run the browser relay on the current small ECS instance. Chrome can exhaust memory and affect SSH, Docker, OpenClaw, and the main AliECS stack.

## ECS Bridge

Deploy the bridge from this repository:

```bash
cd /root/AliECS
sudo bash deploy/openclaw-bridge/install.sh
```

Runtime config lives at:

```text
/opt/openclaw-bridge/webdock.env
```

Recommended production values:

```env
WEB_DOCK_BASE_URL=http://127.0.0.1:11800/v1
WEB_DOCK_API_TOKEN=replace_with_long_random_api_token
WEB_DOCK_MODEL=browser-chatgpt
WEB_DOCK_TIMEOUT_SECONDS=180
WEB_DOCK_FALLBACK_MESSAGE=ChatGPT 浏览器暂不可用，请稍后再试。
```

`127.0.0.1:11800` is the ECS side of the reverse SSH tunnel created by the laptop. ECS does not need to join Tailscale for this path.

Restart after changing config:

```bash
sudo systemctl restart openclaw-bridge.service
```

Verify on ECS:

```bash
curl -fsS http://127.0.0.1:11800/healthz
curl -fsS http://127.0.0.1:18080/v1/models
curl -fsS http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"browser-chatgpt","messages":[{"role":"user","content":"请只回复：bridge-ok"}],"stream":false}'
```

## Prompt Handling

OpenClaw sends a large runtime context to the model. The bridge intentionally does not forward that full context to the browser relay.

The bridge forwards only:

- a short system prompt for the ChatGPT browser
- the last real user message

It removes the OpenClaw `Conversation info (untrusted metadata)` prefix before calling WebDock. This keeps the ChatGPT page clean and avoids confusing the browser session with internal OpenClaw instructions.

## OpenClaw Provider

OpenClaw should call the bridge from inside its gateway container:

```json
{
  "providers": {
    "wechat-bridge": {
      "baseUrl": "http://host.docker.internal:18080/v1",
      "apiKey": "local-placeholder",
      "api": "openai-completions",
      "models": [
        {
          "id": "echo",
          "name": "微信本地桥接",
          "reasoning": false,
          "input": ["text"]
        }
      ]
    }
  }
}
```

The OpenClaw model name may remain `wechat-bridge/echo`; the ECS bridge maps the request to `browser-chatgpt` when WebDock is configured.

## Failure Behavior

If WebDock is offline, busy, timed out, or not logged in, the bridge returns:

```text
ChatGPT 浏览器暂不可用，请稍后再试。
```

This avoids blocking OpenClaw or destabilizing ECS.
