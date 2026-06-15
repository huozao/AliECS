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

## 2026-06-15 isolation fix verification

### Root cause fixed

The live OpenClaw Feishu DM shape did not match the synthetic bridge test shape:

- top-level metadata had `peer_id=user:ou_28d4f058cbd2a13f3fcc6fd575023e8e`, `message_id=om_...`, `chat_type=private`;
- no explicit `channel=feishu` field was present;
- the user text began with `Sender (untrusted metadata):` and a following `[message_id: ...]` line.

Bridge image `V20260615153` fixes that by detecting Feishu from `om_`, `ou_`, and `oc_` identifiers, stripping `user:` / `chat:` prefixes for the lane peer, and stripping the Feishu `Sender` envelope plus leading `[message_id: ...]`.

### Code and release

- PR: `https://github.com/huozao/AliECS/pull/112`
- Merge commit: `92f1d52d1b455a58186fd8f6d99aff2caadf72eb`
- PR checks: `validate`, `migration-dry-run`, `update-pr-body` all passed.
- Release workflow: `release-deploy` run `27551508399` succeeded.
- Bridge image: `ghcr.io/huozao/openclaw-bridge:V20260615153`
- Bridge image digest from build log: `sha256:3e2127eb1d85c08ac441058ef9d7079a4a99db98702400c81a9bbb693011e76f`

### Local tests

```powershell
$env:PYTHONPATH='.'; pytest tests/test_openclaw_bridge.py -v
```

Result: `30 passed`.

```powershell
$env:PYTHONPATH='.'; pytest tests/test_routing_api.py -v
```

Result: `3 passed`.

```powershell
$env:PYTHONPATH='.'; pytest tests/test_feishu_lane_routing.py tests/test_lane_routing.py -v
```

Result in `webdock`: `13 passed`.

### ECS bridge deployment

Deployed on ECS with:

- `/root/infra/server/.env`: `OPENCLAW_BRIDGE_TAG=V20260615153`
- container: `openclaw-bridge|ghcr.io/huozao/openclaw-bridge:V20260615153|Up`
- health: `curl -fsS http://127.0.0.1:18080/v1/models` returned OK
- restart count: `RestartCount=0`

### webdock Feishu project mapping

Wrote `/var/lib/webdock/browser_data/feishu_projects.json` on the old PC:

```json
{
  "lanes": {
    "ou_28d4f058cbd2a13f3fcc6fd575023e8e": {
      "name": "hao (Lark)",
      "project_url": "https://chatgpt.com/g/g-p-6a2ffe0bac248191988612d9081dd6b1-lark/project"
    }
  }
}
```

Restarted `webdock`; `100.97.176.57:18000/healthz` returned `{"ok":true,"service":"webdock"}`. The file passes `python3 -m json.tool`; container status is `running`, `restart=0`.

### Synthetic end-to-end proof

Feishu equivalent POST to `127.0.0.1:18080/v1/chat/completions` used token `feishu-synth-clean-220925`.

Bridge trace:

- `batch_key=feishu:ou_28d4f058cbd2a13f3fcc6fd575023e8e`
- `peer_id=ou_28d4f058cbd2a13f3fcc6fd575023e8e`
- `message_id=om_codex_feishu-synth-clean-220925`
- `text_len=40`

webdock archive:

- `lane.key=feishu:ou_28d4f058cbd2a13f3fcc6fd575023e8e`
- `lane.project=Feishu`
- `lane.target_url=https://chatgpt.com/g/g-p-6a2ffe0bac248191988612d9081dd6b1-lark/c/...`
- `inbound.text=hao: 合成验证：请只回复 feishu-synth-clean-220925`
- no `Sender (untrusted metadata)` and no `[message_id: ...]`
- `status=ok`, outbound text `feishu-synth-clean-220925`

WeChat equivalent POST used token `wechat-synth-ok-221113`.

Bridge trace:

- `batch_key=default|private|o9cq80whD47YZs0xR1Y9Ih8rdVnc@im.wechat`
- `wechat_account=default`
- `peer_id=o9cq80whD47YZs0xR1Y9Ih8rdVnc@im.wechat`

webdock archive:

- `lane.key=wechat:default:private:o9cq80whD47YZs0xR1Y9Ih8rdVnc_im.wechat`
- `lane.project=WeChat-default`
- `lane.target_url=https://chatgpt.com/g/g-p-6a1d3d0e289081918514df104d409ffd-weixin-a/c/...`
- `status=ok`, outbound text `wechat-synth-ok-221113`

### Remaining manual check

True phone-side Feishu/WeChat DM sending still requires a human. The synthetic requests used the real captured OpenClaw metadata shape and verified the bridge plus webdock path; the only remaining optional check is that the human sees the rich Feishu reply in the Feishu client.
