# 飞书一对一通道验证记录（2026-06-14）

## 已完成代码侧

- `openclaw_bridge.py` 已识别 `channel=feishu/lark` 与 `open_id`，转发给 webdock 时保留：
  - `channel=feishu`
  - `peer_id=<open_id>`
  - `chatgpt_project=Feishu`
- bridge 批处理 key 使用 `feishu:<open_id>`，避免与微信 peer 串线。
- webdock `LaneContext` 支持飞书 lane key：`feishu:<peer_id>`。
- webdock `LaneRouter` 支持同时读取：
  - `wechat_projects.json`
  - `feishu_projects.json`

## 已跑离线验证

```powershell
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py -v
```

结果：`27 passed`。

```powershell
pytest tests/test_feishu_lane_routing.py tests/test_lane_routing.py tests/test_chat_lane_scheduler.py -v
```

结果：`21 passed`。

## ECS 只读实时检查

```bash
ssh aliecs "docker ps --format '{{.Names}} {{.Status}}' | grep -iE 'openclaw|bridge'"
```

结果：

- `openclaw-bridge Up 2 days`
- `openclaw-openclaw-gateway-1 Up 2 hours (healthy)`

```bash
ssh aliecs "docker inspect openclaw-openclaw-gateway-1 --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -E 'FEISHU_APP_SECRET|FEISHU|LARK' | sed -E 's/=.*/=__SET__/g'"
```

结果：

- `FEISHU_APP_ID=__SET__`
- `FEISHU_APP_SECRET=__SET__`

```bash
ssh aliecs "docker logs --since 5m openclaw-openclaw-gateway-1 2>&1 | grep -iE 'feishu|lark|websocket' | tail -50"
```

结果：命令成功，但最近 5 分钟没有匹配输出。

## ⚠️ 人工/ops 步骤

1. 在飞书开放平台确认机器人已启用事件订阅，并订阅一对一消息事件。
2. 确认事件回调/长连接模式与当前 OpenClaw `channels.feishu` 配置一致。
3. 用飞书给机器人发送一条 DM。
4. 在 ECS 检查：

```bash
ssh aliecs "docker logs --since 10m openclaw-openclaw-gateway-1 2>&1 | grep -iE 'feishu|lark|websocket|open_id|message' | tail -100"
ssh aliecs "docker logs --since 10m openclaw-bridge 2>&1 | grep -iE 'feishu|open_id|bridge_request_trace' | tail -100"
```

5. 在 webdock archive 中确认 lane key 为 `feishu:<open_id>`，且没有落到 `wechat:*`。
6. 将确认后的飞书 `peer_id/open_id` 填入企微A管理面板 `飞书用户清单`，并同步生成 `feishu_projects.json`。
