# Runbook：飞书 ↔ ChatGPT 链路排障

## 链路图（排障先定位卡在哪一段）

```
飞书用户
  → 飞书开放平台（长连接，⛔后台绝不要切 webhook，会顶掉长连接）
  → OpenClaw（aliecs /root/openclaw，飞书插件）
  → openclaw-bridge（aliecs 容器，源码 deploy/openclaw-bridge/openclaw_bridge.py）
  → 127.0.0.1:11800（aliecs，webdock-failover-proxy 主备路由）
  → 反向隧道（webdock 设备 webdock-ecs-tunnel.service 的 ssh -R）
  → WebDock API :18000（webdock2 主 / webdock1 备）
  → Chrome / ChatGPT 网页
```

主备判定权威：aliecs `/etc/default/webdock-failover-proxy`；实际来源看响应头 `X-Webdock-Device` / `X-Webdock-Route`。

## 日志位置

| 位置 | 看什么 |
|---|---|
| aliecs `docker logs openclaw-bridge` | bridge 收发、`webdock unavailable: timed out`、飞书 API 报错 |
| bridge 健康端点 `127.0.0.1:18080/v1/models`（aliecs 上 curl） | bridge 是否活 |
| webdock 设备 `/var/log/webdock/archive/<UTC日期>.jsonl` | 每对话一行全量收发存档；查 `status` 和 `outbound.chars` |
| chain-logger（infra/server/chain-logger） | 全链路断点定位 |
| 容器内 `/app/logs/api.log`（WebDock） | 路由同步等警告（不在 docker logs 里） |

## 症状表

| 症状 | 先查 | 已知根因史 |
|---|---|---|
| 完全没回复 | ① aliecs `ss -tlnp \| grep 11800` 端口是否被干净绑定 ② webdock2 WSL 是否活（容器 Up 时长 < 命令年龄 = 假活） | 隧道掉线占端口（2026-06-13 已加 sshd ClientAlive 15/3）；Win 重启后 WSL 保活未起（07-12 已改开机+S4U） |
| 收到「暂不可用」但怀疑其实答了 | 存档查该条 inbound：`status=ok` 有 outbound = 回程黑洞，非 WebDock 慢；算 bridge flush 时间+320s 是否=超时时刻 | 同上隧道问题；消息无法补送，需用户重发 |
| 回复只有半截/只有开场白 | 存档 `outbound.chars`；WebDock detector 完成判定 | ⛔ stop 按钮(`data-testid='stop-button'`)是完成判定权威信号，别改回以 streaming 为准 |
| 回复图片变成链接 | bridge 环境变量 FEISHU_APP_ID/SECRET 是否在 | 缺凭据静默退 fallback；补后必须 force-recreate（restart 不重读 env_file） |
| 图改图只回"Edit"/文件名 | imagegen_pending 窗口、预览层兜底、copy 按钮信号 | 07-18 已修（webdock 6550a70+c1bd76a+c9bf9a5） |
| 串频道/消息进错项目 | bridge channel 识别、`feishu_projects.json`、Sender 信封剥离 | 06-15 已修（PR#118/#119）；真实 metadata 是 `peer_id:"user:ou_…"` 无 channel 字段 |
| 多图消息后全线卡死 | WebDock 单 worker 被重请求堵死；healthz 是假绿（只探 /healthz） | 13 图请求 142-153s 堵死单 worker（06-23） |
| 开机后收不到回复 | webdock 设备 Chrome 是否卡「恢复页面」提示 | 人工关浏览器→自动重开干净 Chrome 即自愈（勿自动登录） |
| 卡片格式乱/表格丢失 | lark_md 不认 GFM 表格/`##`/引用 | 表格必须截图、标题转粗体、引用转 `▎`（卡片合成在 bridge） |
| /新对话 /模式 不生效 | 群表「对话模式」列类型、sender 前缀剥离 | 旧构建误建 Checkbox 列（PR#165 加 reconcile_type）；粘性状态优先级链已修（PR#177） |

## 验证命令

```bash
ssh aliecs "curl -s 127.0.0.1:18080/v1/models"          # bridge 活性
ssh aliecs "ss -tlnp | grep 11800"                       # 隧道端口
ssh aliecs "curl -sI http://127.0.0.1:11800/healthz"     # 看 X-Webdock-Device 判主备
ssh webdock2 "wsl -d Ubuntu-24.04-WebDock -- docker ps"  # WebDock 容器状态
```

## 已知坑（改代码前必读）

- bridge 换镜像标签：写 `OPENCLAW_BRIDGE_TAG` 后必须先 `docker rm -f` 再 compose up；**bridge 换码永远手动 cutover**，多会话并行时先查当前 tag 再 cutover（曾发生 232 盖 233）。
- 改 bridge env 后 `restart` 无效，必须 force-recreate。
- 新 webdock 节点必须复制 `runtime.json`，否则飞书图/表全丢。
- OpenClaw 的 dispatch→bridge 延迟 0.9-2.4s 属正常（处理中占位卡即为此设计）。
- bridge 单测：`tests/test_openclaw_bridge.py`；mock 形状必须对齐真实 OpenClaw 输出（合成形状掩盖过串频道 bug）。
