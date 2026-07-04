# 飞书 rule 表运行时控制 + 尾泡语义化(诊断/完结标记)+ 占位卡措辞 设计

日期：2026-07-04
状态：设计稿（已评审通过，待写实现计划）
范围：`AliECS/deploy/openclaw-bridge/openclaw_bridge.py`（bridge，单文件，走 AliECS PR）+ **infra 仓** `infra/server/compose.bridge.yml`（1 行，独立小改，可选）
关联：延续 [[feishu-processing-card-plan]]（占位卡已上线，开关 ON）。本设计解决占位卡上线后暴露的「空泡」观感问题，并把飞书运行时开关收进多维表。

## 1. 背景与要解决的问题

占位卡上线后（PR#148），每条飞书回复后面都跟着**一条多余的消息**：群里显示为只有引用抬头 `▎hao: <原文>`、正文空白；私聊里完全空白。

### 已核实的根因（代码级，三处证据闭合）

| 事实 | 证据 |
|---|---|
| 飞书对 bridge 用**非流式** | OpenClaw 配置 `channels.feishu.streaming=false`（网关 `openclaw.json`），排除 keepalive 零宽空格 |
| bridge 对 NO_REPLY 返回 `content=""` | `openclaw_bridge.py:2824`（`content = "" if reply == NO_REPLY else reply`），`:2832` 塞进 completion |
| OpenClaw `automatic` 模式对空串也投递 | 网关 `messages.visibleReplies` 未设=默认 `automatic`，"每轮把最终助手文本（含空串）投递出去"（`config-channels.md`） |

即：**OpenClaw 每次 dispatch 必发且只发一条最终回复**。bridge 把真答案做成飞书交互卡片**旁路直发**（07-01 有序卡设计），于是 OpenClaw 那条"必发回复"就成了多余的第二条——07-01 前它填的是 `🖼️ 图片已发送`，PR#142 改成 NO_REPLY 后就空了。

**时序**：OpenClaw 调 bridge 的 HTTP 阻塞等待整轮（占位 + webdock + 发卡）；bridge 在调用内部先发卡、返回后 OpenClaw 才补发那条——所以尾泡**必然在卡片之后**，无法靠 bridge 调时序消除。

### 为什么不走"消除"路线

- 方案 X+（改 OpenClaw 核心 automatic 模式让空回复不发）：动 vendored 核心 + 重建网关镜像，超出 bridge 单文件范围。
- 方案 Y（bridge 早返回 + 后台异步发卡）：丢单卡演进、需后台化 + 元数据搬迁 + 并发语义变化，风险高。
- `visibleReplies: message_tool`：按会话类型全局生效，会把**微信打哑**（微信靠 OpenClaw 发文本），否决。

**本设计选"语义化"路线**：那条尾泡既然 OpenClaw 必发、又躲不掉，就**让它永远有意义**——要么是诊断信息，要么是一句平静的完结标记。不碰 OpenClaw、不改时序、不误伤微信。

## 2. 目标 / 非目标

**目标**
1. 把飞书运行时开关（`处理中卡片`、`调试尾注`）收进现有多维表「规则配置表」的全局层，飞书里改一下即时生效，不用重建容器。
2. 尾泡语义化：`调试尾注 ON` → 一行链路诊断；`OFF` → 一句平静的完结标记 `🌿 回复完毕`。空泡问题就此关闭。
3. 优化占位卡措辞（ACK / REMIND）。

**非目标**
- 不做 X+/Y（彻底消除尾泡）。语义化后尾泡不再是"废泡"，无需消除。
- 不改企业微信通道。
- 不改 `PendingBatch.merge` 文本合并（既有坑，范围外）。
- 不动"仅记录 / 非领队 / 新对话"这些早 `NO_REPLY` 静默路径（它们本就该无标记）。

## 3. 已核实事实基线（补充）

| 事实 | 证据 |
|---|---|
| 一条回复 = 一张卡，无分卡逻辑 | `build_feishu_card` 拼一张卡；`deliver_feishu_*` 只发一次；无 chunk/split-by-length |
| 图片单独上传，卡里只放 img_key | `feishu_upload_image` → 卡片体积主要是文字 |
| rule 表（规则配置表）当前**只写不读** | `ensure_feishu_default_rule_record`（`:1290`）建默认记录；运行时无 `table_id("rule")` 读取 |
| 运行时被读的是 group 表 | `feishu_group_reply_policy`（`:1802`）按 chat_id 读 group 表，缓存 `FEISHU_GROUP_POLICY_CACHE_SECONDS`(10min)，`/admin/invalidate-feishu-group-policy`(`:2748`) 热失效 |
| rule 表 global-default 字段 | `规则编号/规则名称/规则对象类型/是否启用/是否记录全量消息/回复模式/是否允许图片/是否允许文件/是否需要审核/每日最大请求数/敏感群标记/备注`（`:1297-1310`） |
| WebDockResult 结构 | `WebDockResult(reply, metadata=None, footer=None)`（`:147`），footer 含 device/route/elapsed_seconds |
| bridge tag 容器内读不到 | `compose.bridge.yml` 的 `${OPENCLAW_BRIDGE_TAG}` 仅用于镜像插值，未进容器 environment/env_file |

## 4. 总体设计

### 4.1 rule 表 → 运行时控制中枢（全局层）

**多维表侧**：「规则配置表」`global-default` 记录新增两个布尔字段：
- `调试尾注`（默认 `true`）
- `处理中卡片`（默认 `true`，匹配当前线上已开状态）

`ensure_feishu_default_rule_record` 扩展为幂等：建默认记录时带这两字段；若 `global-default` 已存在但缺字段，则 `update_feishu_bitable_record` 补上（不覆盖用户已改的值——仅补缺失键）。

**bridge 侧**：新增全局读取
```
feishu_global_rule_policy() -> dict   # {"调试尾注": bool, "处理中卡片": bool}
```
- 读 rule 表 `global-default`（`find_feishu_bitable_record(table_id("rule"), "规则编号", "global-default")`）。
- 缓存：复刻 `feishu_group_reply_policy` 范式（模块级 dict + Lock + TTL=`FEISHU_GROUP_POLICY_CACHE_SECONDS`）；缓存 key 固定 `"global"`。
- **回退矩阵（best-effort，绝不阻断答案）**：
  - 无 rule 表 / 无凭据 / 读失败 / 缺字段 → 回退到对应 env 默认：
    - `处理中卡片` → `OPENCLAW_BRIDGE_PROCESSING_CARD`（现有，默认关；线上 env 现为 1）
    - `调试尾注` → `OPENCLAW_BRIDGE_DEBUG_TRAILER`（新增，默认 **ON**）
  - rule 表字段存在 → 表值优先于 env。
- 热失效：`/admin/invalidate-feishu-group-policy` 端点顺带清 `"global"` 缓存（飞书表自动化改字段后即时生效）。

消费点：
- `processing_card_enabled()` 改为：先查 `feishu_global_rule_policy()["处理中卡片"]`，缺失回退现有 env 逻辑。
- 新增 `debug_trailer_enabled()`：查 `feishu_global_rule_policy()["调试尾注"]`，缺失回退 env（默认 ON）。

### 4.2 尾泡语义化

替换点：**`build_reply` 的"卡片投递成功 NO_REPLY 出口"**（成功段 `finalize_placeholder` 之后、`return reply` 之前）。

```
# 伪代码（仅飞书 + reply == NO_REPLY，即真发了卡的成功出口）
if (write_details.get("metadata") or {}).get("channel") == "feishu" and reply == NO_REPLY:
    reply = build_feishu_trailer(write_details)
```

`build_feishu_trailer(details) -> str`（所需数据全从 details + 模块状态(`_inflight_counts`) + env 读取）：
- `debug_trailer_enabled()` 为真 → 诊断行：
  ```
  🔧 bridge=V20260703218 req=8da08f41 conv=…/c/abc | busy=1 lane=feishu:oc_b39… | model=browser-chatgpt timeout=320s patched=yes
  ```
  - 核心链路：`bridge=`(env `OPENCLAW_BRIDGE_TAG`，缺失显示 `unknown`) / `req=`(request_id) / `conv=`(metadata `chatgpt_conversation_url`，取尾段) 
  - 并发态：`busy=`(此刻本 lane 在飞计数，取 `_inflight_counts.get(lane_key, 0)`——trailer 在 `finally` 递减前构造，故含本请求，≥2 即有追问重叠) / `lane=`(lane_batch_key)
  - webdock 细节：`model=`(env `WEB_DOCK_MODEL`) / `timeout=`(webdock_timeout()) / `patched=`(占位卡是否存在，取 `bool(write_details.get("feishu_placeholder_msg_id"))`——此刻 reply 已是 NO_REPLY，代表卡片经 patch 就地更新)
- 否 → 完结标记：`os.getenv("OPENCLAW_BRIDGE_DONE_MARKER", "🌿 回复完毕")`

**边界**：
- 只替换"真发了卡"的成功出口。`feishu_should_send_chatgpt=false`（仅记录）、`maybe_batch_request` 非领队、`/新对话` 等早 `NO_REPLY` 出口**不替换**，保持静默（那些本就没答，不该带完结标记）。
- 错误分支（HTTPError / URLError 等）已返回 `diagnostic_message` 文本，OpenClaw 会投递，**不叠加**尾注。
- 全程 best-effort：trailer 构造异常 → `log_line` 并回退返回 `NO_REPLY`（宁可空泡也不阻断）。

### 4.3 占位卡措辞

改 `send_processing_card` 用到的两个默认文案常量（env 仍可覆盖）：
- `DEFAULT_PROCESSING_ACK_TEXT` = `📨 已投递到 ChatGPT，正在生成（约 20–60 秒）。答案会直接更新到这张卡片，请勿重复提问 🙏`
- `DEFAULT_PROCESSING_REMIND_TEXT` = `⚠️ 上一条还在 ChatGPT 处理中，这条已排队。请等上面那张卡片出结果再问，连续提问会拖慢每一条。`

env 覆盖键不变：`OPENCLAW_BRIDGE_PROCESSING_ACK_TEXT` / `..._REMIND_TEXT`。

### 4.4 compose 附带改动（infra 仓，可选，为尾注显示 tag）

⚠️ `compose.bridge.yml` **在 infra 仓**（`infra/server/compose.bridge.yml`），不在 AliECS。这是一个独立的小改，**可选**：`build_feishu_trailer` 读不到 `OPENCLAW_BRIDGE_TAG` 时优雅降级为 `bridge=unknown`，不报错、不阻断。

infra 仓 `compose.bridge.yml` 的 `environment:` 块加一行让 bridge 进程读到自己运行标签：
```yaml
      OPENCLAW_BRIDGE_TAG: ${OPENCLAW_BRIDGE_TAG:-unknown}
```
此改动**不进 AliECS PR**，作为部署收尾在 infra 仓单独提交（推三 remote），随下次 bridge cutover 生效。AliECS 侧代码对该 env 缺失完全容错。

## 5. 数据流（一条飞书回复，处理中卡片+调试尾注 均 ON）

```
用户发消息
  └ OpenClaw 收到 → 阻塞调 bridge /v1/chat/completions
      └ build_reply:
          ├ 读 feishu_global_rule_policy()（缓存/回退 env）
          ├ 处理中卡片 ON → send_processing_card(ACK 文案) 直发飞书 ★占位卡⏳
          ├ call_webdock (…s)
          ├ deliver_feishu_*/finalize_placeholder → PATCH 占位卡成答案 ★卡片=答案，reply=NO_REPLY
          ├ 尾泡语义化：reply = build_feishu_trailer(...)  # 诊断行 或 🌿回复完毕
          └ return 该文本
      └ bridge HTTP 返回 content=该文本
  └ OpenClaw 投递该文本 ★尾泡（有意义）
最终：一张卡(⏳→答案) + 一条尾泡(诊断行 / 🌿回复完毕)
```

## 6. 失败与降级矩阵

| 情况 | 处置 | 结果 |
|---|---|---|
| rule 表无 / 无凭据 / 读失败 / 缺字段 | 回退 env 默认 | 处理中卡片按 env、调试尾注默认 ON、行为不变 |
| trailer 构造抛异常 | log + 回退 `NO_REPLY` | 退回空泡，答案不受影响 |
| 占位卡发送/ patch 失败 | 既有 best-effort 路径（feishu_put_card 降级新发 / finalize 兜底） | 答案照送，不受本设计影响 |
| 超长纯文本触顶飞书卡片上限 | 发卡/ patch 失败 → 既有 fallback 走 OpenClaw 文本 | 罕见降级；此时 reply≠NO_REPLY，尾注不替换（不叠加） |
| 仅记录 / 非领队 / 新对话 | 早 `NO_REPLY` 返回，不进替换点 | 保持静默，无尾注 |

## 7. 不变量

1. **不接表/读失败时行为与今天一致**（回退 env）。
2. **一条回复 = 一张卡**：本设计不改卡片构建/投递，单卡演进机制不变。
3. **尾泡永远有意义**（诊断行或完结标记），不再空；但静默路径（仅记录等）仍无尾泡。
4. **best-effort**：任何新增读取/构造失败都不阻断答案送达。
5. **企业微信不受影响**（所有新逻辑门控 `channel == "feishu"`）。

## 8. 配置总览

| 键 | 位置 | 默认 | 作用 |
|---|---|---|---|
| `处理中卡片` | rule 表 global-default | true | 占位卡总开关（表值优先） |
| `调试尾注` | rule 表 global-default | true | 尾泡=诊断行 / 完结标记 |
| `OPENCLAW_BRIDGE_PROCESSING_CARD` | env | 关(线上现 1) | 处理中卡片回退默认 |
| `OPENCLAW_BRIDGE_DEBUG_TRAILER` | env | ON | 调试尾注回退默认 |
| `OPENCLAW_BRIDGE_DONE_MARKER` | env | `🌿 回复完毕` | 完结标记文案 |
| `OPENCLAW_BRIDGE_PROCESSING_ACK_TEXT` / `..._REMIND_TEXT` | env | 见 §4.3 | 占位卡措辞 |
| `OPENCLAW_BRIDGE_TAG` | 容器 env（compose 新传） | unknown | 尾注显示 bridge tag |

## 9. 测试计划（红绿，加在 tests/test_openclaw_bridge.py）

- `feishu_global_rule_policy`：表值优先 / 缺字段回退 env / 读失败回退 env / 缓存命中不重复扫表 / invalidate 清 global 缓存。
- `debug_trailer_enabled` / `processing_card_enabled`：表 vs env 优先级、默认值。
- `build_feishu_trailer`：ON→诊断行含各字段；OFF→完结标记；env 覆盖 DONE_MARKER 生效；bridge tag 缺失显示 unknown。
- `build_reply`：飞书+发卡成功出口把 NO_REPLY 替换成 trailer（ON 诊断/OFF 标记各一例）；仅记录/非领队仍返 NO_REPLY 不带尾注；错误分支不叠加尾注；trailer 构造异常回退 NO_REPLY。
- 措辞：ACK/REMIND 默认值更新；env 覆盖仍生效。
- 回归：现有占位卡/卡片投递/batching/metadata 用例不回归。

## 10. 部署

- AliECS 走 **PR**（不直推 main）。
- bridge 镜像重建 + **手动 cutover**（bridge-cutover workflow）。
- rule 表两字段：`ensure_feishu_default_rule_record` 会自动补建；或飞书侧手动加字段。
- 上线后飞书表把 `调试尾注` 关一下验证 → 尾泡变 `🌿 回复完毕`；开回去 → 变诊断行；改 `处理中卡片` 验证占位卡开关热生效。

## 11. 已知边角 / 待办

- 超长纯文本触顶飞书卡片上限的降级路径（走 OpenClaw 文本、可能被拆多条）本设计不优化，记为已知边角。
- 静默路径（仅记录）若 OpenClaw 仍对空串投递空泡，属独立议题；本设计不处理（那些消息 bot 本就没答）。
- `调试尾注` 默认 ON = 群内每条回复带一行含 conv url/内部 id 的诊断,群成员可见——已与用户确认，个人小群可接受。
