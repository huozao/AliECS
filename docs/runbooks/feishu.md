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
| 全链路每条都失败、bridge `chain_result` 全是 `http_500` 且十几秒就返回 | WebDock `api.log` 是否 `TargetClosedError`；容器内 Chrome 启动时间是否晚于 api 进程（`ps -eo pid,lstart,args`） | Chrome 被重启后 api 仍抓着死句柄，`started` 只判 `_page is not None` 导致永不重连；07-25 已修（webdock `29c163c`，`started` 加 `is_connected()` 校验）。应急：容器内 `POST /browser/detach` 再 `/browser/attach`，CDP 模式不会关 Chrome、不碰登录态 |
| supervisord 报 `exited: chrome (exit status 0; expected)`，Chrome 无故重启 | 前一条请求是否卡满 310s 硬顶触发车道重建（`api.log` 找 `RESPONSE_TIMEOUT ... lane reset`） | 车道重建先关旧 tab 再开新 tab，关掉的是最后一个窗口 → Chrome 干净自退，随后 `new_page` 报 `Failed to open a new tab`；07-25 已修（webdock `08c4550` 改为先开新 tab） |
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

- bridge 换码：合并到 main 即自动 cutover——release-deploy 发现 `deploy/openclaw-bridge/**` 的内容树 hash 变了，构建完调用 `bridge-cutover`。tree hash 没变则完全不触发（同一镜像标签，切了也只是白重启咽喉服务）。workflow 按所选 Git ref 自动解析内容镜像与 OCI digest，原子切换、健康检查、失败回滚。回滚/重切/切非 main ref 用 `bridge-cutover` 的手工 `workflow_dispatch`（confirmation 填 `CUTOVER_TXECS`）。禁止登机手改 `.env`；并发由 workflow concurrency 串行化。
- 改 bridge env 后 `restart` 无效，必须 force-recreate。
- 新 webdock 节点必须复制 `runtime.json`，否则飞书图/表全丢。
- OpenClaw 的 dispatch→bridge 延迟 0.9-2.4s 属正常（处理中占位卡即为此设计）。
- bridge 单测：`tests/test_openclaw_bridge.py`；mock 形状必须对齐真实 OpenClaw 输出（合成形状掩盖过串频道 bug）。
