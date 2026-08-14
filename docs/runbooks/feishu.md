# Runbook：飞书 ↔ ChatGPT 链路排障

## 链路图（排障先定位卡在哪一段）

```
飞书用户
  → 飞书开放平台（长连接，⛔后台绝不要切 webhook，会顶掉长连接）
  → OpenClaw（txecs 容器，飞书插件）
  → openclaw-bridge（txecs 容器，源码 deploy/openclaw-bridge/openclaw_bridge.py）
  → 127.0.0.1:11800（txecs，webdock-failover-proxy 主备路由）
  → POST /v1/chat/jobs（快速返回 job_id）
  → 反向隧道（webdock 设备 webdock-ecs-tunnel.service 的 ssh -R）
  → WebDock API :18000（webdock2 主 / webdock1 备）
  → Chrome / ChatGPT 网页
  ← GET /v1/chat/jobs/{job_id}（固定原接单节点，短轮询；`progress` 驱动处理卡片阶段更新）
```

主备判定权威：txecs `/etc/default/webdock-failover-proxy`；实际来源看响应头 `X-Webdock-Device` / `X-Webdock-Route`。

## 日志位置

| 位置 | 看什么 |
|---|---|
| txecs `docker logs openclaw-bridge` | bridge 收发、`webdock unavailable: timed out`、飞书 API 报错 |
| bridge 健康端点 `127.0.0.1:18080/v1/models`（txecs 上 curl） | bridge 是否活 |
| webdock 设备 `/var/log/webdock/archive/<UTC日期>.jsonl` | 每对话一行全量收发存档；查 `status` 和 `outbound.chars` |
| chain-logger（infra/server/chain-logger） | 全链路断点定位 |
| 容器内 `/app/logs/api.log`（WebDock） | 路由同步等警告（不在 docker logs 里） |

## 症状表

| 症状 | 先查 | 已知根因史 |
|---|---|---|
| 完全没回复 | ① txecs `ss -tlnp \| grep 11800` 端口是否被干净绑定 ② webdock2 WSL 是否活（容器 Up 时长 < 命令年龄 = 假活） | 隧道掉线占端口（2026-06-13 已加 sshd ClientAlive 15/3）；Win 重启后 WSL 保活未起（07-12 已改开机+S4U） |
| 收到「暂不可用」但怀疑其实答了 | 存档查该条 inbound：`status=ok` 有 outbound = 回程黑洞，非 WebDock 慢；算 bridge flush 时间+320s 是否=超时时刻 | 同上隧道问题；消息无法补送，需用户重发 |
| 回复只有半截/只有开场白 | 存档 `outbound.chars`；WebDock detector 完成判定 | ⛔ stop 按钮(`data-testid='stop-button'`)是完成判定权威信号，别改回以 streaming 为准 |
| 回复图片变成链接 | bridge 环境变量 FEISHU_APP_ID/SECRET 是否在 | 缺凭据静默退 fallback；补后必须 force-recreate（restart 不重读 env_file） |
| 图改图只回"Edit"/文件名 | imagegen_pending 窗口、预览层兜底、copy 按钮信号 | 07-18 已修（webdock 6550a70+c1bd76a+c9bf9a5）；08-14 因 08-12 的协议快通道绕过 scaffold 闸门而回归，已二修，详见 `webdock/docs/runbooks/browser.md`「图改图的完成判据只有一个能信」 |
| 串频道/消息进错项目 | bridge channel 识别、`feishu_projects.json`、Sender 信封剥离 | 06-15 已修（PR#118/#119）；真实 metadata 是 `peer_id:"user:ou_…"` 无 channel 字段 |
| 全链路每条都失败、bridge `chain_result` 全是 `http_500` 且十几秒就返回 | WebDock `api.log` 是否 `TargetClosedError`；容器内 Chrome 启动时间是否晚于 api 进程（`ps -eo pid,lstart,args`） | Chrome 被重启后 api 仍抓着死句柄，`started` 只判 `_page is not None` 导致永不重连；07-25 已修（webdock `29c163c`，`started` 加 `is_connected()` 校验）。应急：容器内 `POST /browser/detach` 再 `/browser/attach`，CDP 模式不会关 Chrome、不碰登录态 |
| supervisord 报 `exited: chrome (exit status 0; expected)`，Chrome 无故重启 | 前一条请求是否卡满 310s 硬顶触发车道重建（`api.log` 找 `RESPONSE_TIMEOUT ... lane reset`） | 车道重建先关旧 tab 再开新 tab，关掉的是最后一个窗口 → Chrome 干净自退，随后 `new_page` 报 `Failed to open a new tab`；07-25 已修（webdock `08c4550` 改为先开新 tab） |
| 多图消息后全线卡死 | WebDock 单 worker 被重请求堵死；healthz 是假绿（只探 /healthz） | 13 图请求 142-153s 堵死单 worker（06-23） |
| 开机后收不到回复 | webdock 设备 Chrome 是否卡「恢复页面」提示 | 人工关浏览器→自动重开干净 Chrome 即自愈（勿自动登录） |
| 卡片格式乱/表格丢失 | lark_md 不认 GFM 表格/`##`/引用 | 表格必须截图、标题转粗体、引用转 `▎`（卡片合成在 bridge） |
| /新对话 /模式 不生效 | 群表「对话模式」列类型、sender 前缀剥离 | 旧构建误建 Checkbox 列（PR#165 加 reconcile_type）；粘性状态优先级链已修（PR#177） |
| 同群后续消息很快返回 `LANE_BUSY` | WebDock archive 查同一 `lane.key` 的 active 请求和被拒请求 | WebDock 只短等车道锁；被拒消息没有发进 ChatGPT。等待当前任务，或发送 `/新对话` 抢占并重建该车道 |
| 长任务超过 320s 但处理卡片仍更新 | bridge trace 查 job submit/poll；WebDock archive 查最终状态 | 正常：浏览器任务已与 failover-proxy 的单连接 320s 解耦；占位卡轮播持续显示“处理中 + 已等待秒数”，最终结果仍回填原卡片 |

## 验证命令

```bash
ssh txecs "curl -s 127.0.0.1:18080/v1/models"           # bridge 活性
ssh txecs "ss -tlnp | grep 11800"                        # 隧道端口
ssh txecs "curl -sI http://127.0.0.1:11800/healthz"      # 看 X-Webdock-Device 判主备
ssh webdock2 "wsl -d Ubuntu-24.04-WebDock -- docker ps"  # WebDock 容器状态
```

## 已知坑（改代码前必读）

- bridge 换码：合并到 main 即自动 cutover——release-deploy 发现 `deploy/openclaw-bridge/**` 的内容树 hash 变了，构建完调用 `bridge-cutover`。tree hash 没变则完全不触发。workflow 按所选 Git ref 解析内容镜像与 OCI digest，原子更新 txecs 的唯一运行状态 `/srv/internal-stack/release.env`，再由 `txecs-openclaw-bridge.service` 使用 `/srv/openclaw-bridge/docker-compose.yml` 重启、健康检查并失败回滚。回滚/重切/切非 main ref 用 `bridge-cutover` 的手工 `workflow_dispatch`（confirmation 填 `CUTOVER_TXECS`）。禁止登机手改状态文件或另起 compose；并发由 workflow concurrency 串行化。
- 多维表格**不在消息路径上读**。bridge 后台线程按 `FEISHU_BITABLE_SNAPSHOT_REFRESH_SECONDS`（默认 60s）把用到的表全量刷进内存快照，消息只读内存；这个周期是唯一一个「飞书 API 调用量 ↔ 配置生效速度」的旋钮，轮询 N 张表周期 T 秒 = 每天 `86400/T*N` 次请求，60s/4 张表 ≈ 每分钟 4 次。调小只增调用量，不改消息延迟。快照落盘 `/srv/openclaw-bridge/state/bitable-snapshot.json`，容器重启后直接载入。改表生效延迟 = 刷新周期。要立刻生效就 POST `/admin/invalidate-feishu-group-policy`（带 `X-Admin-Secret`），它会清缓存并同步重拉快照。
  - `FEISHU_BITABLE_LIST_CACHE_SECONDS`（默认 900s）不是新鲜度目标，是「刷新线程死了」的兜底岁数，超过就退回同步扫表让真实错误浮出来。
  - 每天 `FEISHU_BITABLE_RECONCILE_AT`（默认 04:00 容器本地时间）全量核对一次，异常告警到飞书群 `oc_84d1130542509e374f7ea20c13d11ca4`。日志关键字 `bitable_reconcile`、`bitable_snapshot_worker_start`。
  - 每轮刷新后 diff 配置表并把改动播到同一个群（`✎ 规则表「x」字段：旧 → 新` + 「已完成全量同步」）。日志关键字 `bitable_config_changed`。**只盯 rule/group/user 三张配置表**：会话表每条消息都在写，纳进来就是每分钟刷屏；用户表/群表里 bridge 自己写回的运行字段（`最近消息时间`/`已用次数`/`@机器人次数`/`关联*`/`上下文摘要`…）同样被 `FEISHU_CONFIG_RUNTIME_FIELDS` 排除。改这个白名单前先想清楚"这个字段是人改的还是流量改的"。
  - 播报静默的两种正常情况：进程刚起第一轮只建基线（磁盘快照过期被丢弃时，否则整表算新增）；单轮改动超过 20 条只列前 20 条 + 报总数。
  - ⚠️ 2026-07-28 实测：改之前群消息扫表 2-3s、私聊 6-9s，是 15s 端到端里最大的一段。改错这里会直接把延迟加回去。
- **端到端延迟分账**（2026-07-28 实测，稳态、纯文本、不含 ChatGPT 生成时间）：飞书→OpenClaw→bridge 约 2s ｜ bridge 内部 1.2s（batch 0.5s + 发占位卡 0.67s）｜ 隧道 RTT 0.09s ｜ WebDock 收请求→文字进输入框 2.4s。合计约 7s。再有人报"慢"，先按这张账对齐是哪一段变了，别凭感觉猜；bridge 段看 `bridge_request_trace`，WebDock 段看 `send_stages`（webdock `docs/runbooks/browser.md`）。
- bitable **写**早就在后台线程里（`bitable-writer`），不在主路径；不要为了"提速"再去动写路径。
- 改 bridge env 后 `restart` 无效，必须 force-recreate。
- 新 webdock 节点必须复制 `runtime.json`，否则飞书图/表全丢。
- OpenClaw 的 dispatch→bridge 延迟 0.9-2.4s 属正常（处理中占位卡即为此设计）。
- bridge 单测：`tests/test_openclaw_bridge.py`；mock 形状必须对齐真实 OpenClaw 输出（合成形状掩盖过串频道 bug）。
- WebDock HTTP 429 会读取错误体：`LANE_BUSY` 的错误码和“已等待/未执行”文案会进入飞书诊断卡片；旧 `BUSY` 仍保留原 browser-lock 文案。
- bridge 默认先提交 WebDock 异步 job，再以不超过 30s 的短 HTTP 查询状态；提交响应必须带合法的 `X-Webdock-Route`，后续固定轮询 `11810`（primary）或 `11811`（standby）。短暂超时/5xx 在总时限内重试；若提交响应丢失，bridge 用确定性 job id 到两节点找回已接单任务。只有 job 接口明确返回 404/405 才降级旧同步接口，避免重复执行。这项粘性不能去掉，否则 standby 接单后 primary 恢复会查到错误节点。
- job 运行期间飞书占位卡轮播不会停止：基础文案/提示文案继续轮换并附 `已等待 Ns`；只有终局答案或诊断卡 patch 时才停止。这样“页面仍在处理”和“结果已完成/失败”在用户侧可见。
- **WebDock 生命周期阶段与轮播提示是同一张卡、同一个 patch 源**。阶段文案由 `set_placeholder_status` 写进轮播状态，顶替基础占位文案坐第一格，提示文案照常轮换：用户既看得到「ChatGPT 页面正在处理」，也看得到「/新对话」「勿重复提问」和等待秒数。⚠️ 阶段回调**不能**自己调 `feishu_patch_card`——非轮播 patch 会触发终局保护掐停轮播，而第一个 phase（`queued`）在提交那一刻就到，等于占位卡刚发出去提示就全灭（08-12 `#301` 引入，08-14 修）。只有这条消息没有轮播（轮播关闭或提示文案为空）时才退回自己 patch。

<!-- 本文点名的符号，改名时本文必须同批更新；校验器会拦 -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:FEISHU_BITABLE_LIST_CACHE_SECONDS -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:FEISHU_BITABLE_RECONCILE_AT -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:FEISHU_BITABLE_SNAPSHOT_REFRESH_SECONDS -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:FEISHU_CONFIG_RUNTIME_FIELDS -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:set_placeholder_status -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:feishu_patch_card -->
