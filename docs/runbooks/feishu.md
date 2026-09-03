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
| 用户 @ 机器人的文本被一起发进了 ChatGPT | 存档 inbound 末尾是否残留 `@<机器人名>`；bridge 是否收到 `raw_metadata.mentions` | 旧实现只剥**开头**的 mention（`^\s*@\S+`），而用户习惯把 @ 放在正文末尾，08-17 实测原样穿透。已改为位置无关：helper 行里的 `If user_id is "ou_x"` 给出机器人自己的 id，`<at user_id=...>` 按 id 精确剥；明文 `@名字` 取自事件 `mentions` 的 name，**因此机器人改名（如改成 ChatGPT）不需要改代码**。拿不到 mentions 时只剥独占一行或首尾的 @，句中的 @同事 一律保留。⚠️ 2026-09-02 起再收一层：只要拿得到机器人 open_id 且它**不在**被 @ 名单里，就一个 @ 都不剥（此前首尾的 @同事 会被误剥）|
| 群里冒出空白气泡（没人 @ 也冒） | bridge trace 该条是不是 `result:"empty"`、`reply_len:0`；OpenClaw 日志同刻是不是 `dispatch complete … replies=1` | 「仅@回复」把消息挡下是对的，错在**回了 content=""，OpenClaw 照样投递**（08-18 实测）。08-18 起改为回 OpenClaw 自己的静默令牌 `NO_REPLY`（`OPENCLAW_SILENT_REPLY_TOKEN`，群聊默认 `silentReply: allow` 才生效）。⚠️ 这个字面量必须和 OpenClaw 的 `SILENT_REPLY_TOKEN` 一字不差，写错就是把令牌本身发进群 |
| 设了「仅@回复」，某条明明 @ 了却没反应 | 消息日志表该条的「是否 @ 机器人」「原始事件 JSON」有没有 `was_mentioned` | **手打或粘贴出来的「@某某」不是 @**（08-18 实测）：只有用 @ 选择器选出来的才带 mention 元数据（事件里有 `was_mentioned`，且 @ 文本会被剥掉）；明文 @ 在飞书眼里就是普通文字，判「未@机器人」是判对的，不是 bug |
| 串频道/消息进错项目 | bridge channel 识别、`feishu_projects.json`、Sender 信封剥离 | 06-15 已修（PR#118/#119）；真实 metadata 是 `peer_id:"user:ou_…"` 无 channel 字段 |
| 全链路每条都失败、bridge `chain_result` 全是 `http_500` 且十几秒就返回 | WebDock `api.log` 是否 `TargetClosedError`；容器内 Chrome 启动时间是否晚于 api 进程（`ps -eo pid,lstart,args`） | Chrome 被重启后 api 仍抓着死句柄，`started` 只判 `_page is not None` 导致永不重连；07-25 已修（webdock `29c163c`，`started` 加 `is_connected()` 校验）。应急：容器内 `POST /browser/detach` 再 `/browser/attach`，CDP 模式不会关 Chrome、不碰登录态 |
| supervisord 报 `exited: chrome (exit status 0; expected)`，Chrome 无故重启 | 前一条请求是否卡满 310s 硬顶触发车道重建（`api.log` 找 `RESPONSE_TIMEOUT ... lane reset`） | 车道重建先关旧 tab 再开新 tab，关掉的是最后一个窗口 → Chrome 干净自退，随后 `new_page` 报 `Failed to open a new tab`；07-25 已修（webdock `08c4550` 改为先开新 tab） |
| 多图消息后全线卡死 | WebDock 单 worker 被重请求堵死；healthz 是假绿（只探 /healthz） | 13 图请求 142-153s 堵死单 worker（06-23） |
| 开机后收不到回复 | webdock 设备 Chrome 是否卡「恢复页面」提示 | 人工关浏览器→自动重开干净 Chrome 即自愈（勿自动登录） |
| 卡片格式乱/表格丢失 | lark_md 不认 GFM 表格/`##`/引用 | 表格必须截图、标题转粗体、引用转 `▎`（卡片合成在 bridge） |
| 群里 @ 的是同事，消息却被送进了 ChatGPT | 消息日志表该条的「是否 @ 机器人」「@对象列表」；`raw_metadata` 里有没有 `mentioned_bot` | `feishu_mentions_bot` 的三条兜底（`mentions` 非空 / helper 文案命中 / 文本含 `<at `）对「@ 了任何人」都为真，2026-09-02 修（PR#353）。OpenClaw 的两行 helper 是 `if (ctx.hasAnyMention)` 注入的，**与 @ 的是谁无关**。⚠️「判据改成拿机器人 open_id 去比对被 @ 的 id 集合」这句 09-03 当天就被推翻——**正文里根本没有机器人的 id**，比对必然落空，反而把 @ 机器人也判成了没 @（详见下方〈群 @ 判定〉）。现在的判据是上游的 `was_mentioned` |
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
- **群 @ 判定：唯一可信的是上游的 `was_mentioned`**（2026-09-02 建，2026-09-03 推翻重写，PR#353→PR#356）。
  先记住到 bridge 的**真实形状**（09-03 从多维表「原始事件 JSON」实抓，别再凭想象写 mock）：

  | 群里发的 | bridge 收到的正文 | metadata 里的关键字段 |
  |---|---|---|
  | `早上好@姜妮娜` | `早上好<at user_id="ou_0545…">姜妮娜</at>` | `is_group_chat: true`，**没有** `was_mentioned` |
  | `早上好@ChatGPT` | `早上好` | `is_group_chat: true`，`was_mentioned: true` |

  - **机器人自己的 mention 在正文里是不存在的**：OpenClaw 把它整个删掉，只有同事的才转成
    `<at user_id="真实 open_id">名字</at>`。所以「拿机器人 open_id 去正文里比对」永远落空——
    ⚠️ 09-02 第一版修复正是这么写的，上线次日早上就把群里的 @ 机器人打哑了。
  - 事件的 **`mentions` 数组根本没传给 bridge**，`mentioned_user_ids` 也没有。当前唯一的信号就是
    `was_mentioned`，而它写作 `ctx.WasMentioned === true ? true : void 0`，**没 @ 机器人时 key 整个消失**。
  - 那怎么把「@ 同事」和「元数据压根没来」分开？看同一个 payload builder 的兄弟字段：
    `is_group_chat` 与 `was_mentioned` 都出自 `buildConversationMentionMetadataPayload`，
    **前者在场就证明这批 mention 元数据到齐了**；再加上 helper 块存在（说明这条消息确实 @ 了谁），
    此时 `was_mentioned` 缺席就等于「@ 的不是我」。判据落在连接处，而不是落在某个值上。
  - **bridge 自己写进 `raw_metadata` 的字段，永远不许参与「上游显式布尔」那一组**。09-03 的事故就是
    自写的 `mentioned_bot: false` 排在 `was_mentioned` 前面，把正确答案盖掉了。
  - helper 文案自带一句字面示例 `<at user_id="...">name</at>`，**解析 at 标签的正则必须限定
    `ou_`/`on_`/`cli_` 前缀**，否则 `"..."` 会被当成一个真 id，让 id 集合永远非空。
  - 兜底保留「元数据全空就当被 @」——宁可多回一次，也不能让群里叫不动机器人。
- bridge 单测：`tests/test_openclaw_bridge.py`；mock 形状必须对齐真实 OpenClaw 输出（合成形状掩盖过串频道 bug）。
- WebDock HTTP 429 会读取错误体：`LANE_BUSY` 的错误码和“已等待/未执行”文案会进入飞书诊断卡片；旧 `BUSY` 仍保留原 browser-lock 文案。
- bridge 默认先提交 WebDock 异步 job，再以不超过 30s 的短 HTTP 查询状态；提交响应必须带合法的 `X-Webdock-Route`，后续固定轮询 `11810`（primary）或 `11811`（standby）。短暂超时/5xx 在总时限内重试；若提交响应丢失，bridge 用确定性 job id 到两节点找回已接单任务。只有 job 接口明确返回 404/405 才降级旧同步接口，避免重复执行。这项粘性不能去掉，否则 standby 接单后 primary 恢复会查到错误节点。
- **UPLOAD_FAILED 是唯一会自动改投备机的失败**（2026-08-17）。WebDock 在附件确认没进输入框时报这个码，并保证「本次请求未发送」，所以重投不会让 ChatGPT 收到两遍同一个问题；bridge 直投 `11811`（standby）重跑一次，日志关键字 `re-sending request ... to the standby`。
  - 其余错误码**一律不重投**：`GENERATION_FAILED` 来自 ChatGPT 自己的服务端（换台机器同样失败），`RESPONSE_TIMEOUT`/`REQUEST_CANCELLED` 那一轮已经发进 ChatGPT 了，重投等于问两遍。
  - ⚠️ 代价是**备机是另一个 Chrome、另一条会话**，重投会在那边新开对话，上下文不延续。之所以可接受：产生这个错误的上传竞态几乎只发生在 `/新对话` 那一轮。
  - 前提是 bridge 能定址备机（`webdock_jobs_url("standby") != webdock_jobs_url("primary")`，即前面挂着 failover-proxy）。不满足时不重投，避免"重试"其实落回同一台。
  - ⚠️ 不要指望 failover-proxy 替你做这件事：它只在**连不上主机**或主机返回 **503 且含 `Chrome not running or CDP attach failed`** 时才切备机。业务失败（HTTP 500 + 结构化错误码）在它眼里是"主机好好的，只是这次没干成"，而且异步 job 模式下失败出现在轮询响应里，proxy 根本看不见。
- job 运行期间飞书占位卡轮播不会停止：基础文案/提示文案继续轮换并附 `已等待 Ns`；只有终局答案或诊断卡 patch 时才停止。这样“页面仍在处理”和“结果已完成/失败”在用户侧可见。
- **WebDock 生命周期阶段与轮播提示是同一张卡、同一个 patch 源**。阶段文案由 `set_placeholder_status` 写进轮播状态，顶替基础占位文案坐第一格，提示文案照常轮换：用户既看得到「ChatGPT 页面正在处理」，也看得到「/新对话」「勿重复提问」和等待秒数。⚠️ 阶段回调**不能**自己调 `feishu_patch_card`——非轮播 patch 会触发终局保护掐停轮播，而第一个 phase（`queued`）在提交那一刻就到，等于占位卡刚发出去提示就全灭（08-12 `#301` 引入，08-14 修）。只有这条消息没有轮播（轮播关闭或提示文案为空）时才退回自己 patch。

<!-- 本文点名的符号，改名时本文必须同批更新；校验器会拦 -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:OPENCLAW_SILENT_REPLY_TOKEN -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:FEISHU_BITABLE_LIST_CACHE_SECONDS -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:FEISHU_BITABLE_RECONCILE_AT -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:FEISHU_BITABLE_SNAPSHOT_REFRESH_SECONDS -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:FEISHU_CONFIG_RUNTIME_FIELDS -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:set_placeholder_status -->
<!-- nav-check-python: deploy/openclaw-bridge/openclaw_bridge.py:feishu_patch_card -->
