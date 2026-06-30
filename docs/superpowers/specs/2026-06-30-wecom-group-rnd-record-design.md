# 企微群研发过程记录：群@消息捕获 → 关联需求 → 写入智能表格

> 来源：2026-06-30 session「智能表同步 图片回写 开发存档」的群消息子项。
> 仓库：AliECS（doc-sync-worker + backend-api，PostgreSQL）。改动走 PR，不直推 main。
> 目标：把「配色&样品需求单」里的需求 + 该需求群里的关键讨论，沉淀成可查的研发过程记录。

## 0. 已验证事实（真机实测，作为真值；不要再推翻重测）

环境：env_profile=`COMPANY_B`，corpid=`ww418fe44020dfee2d`。自建应用「AGI-达」(agentid `1000003`) 走 API；专用智能机器人「项目开发管理助手」走长连接捕获。

### 0.1 智能机器人长连接（捕获通道）——✅ 全链路打通
- WebSocket：`wss://openws.work.weixin.qq.com`。
- 订阅鉴权：发送 `{"cmd":"aibot_subscribe","headers":{"req_id":"..."},"body":{"bot_id":BOT,"secret":SECRET}}` → 返回 `{"errcode":0,"errmsg":"ok"}`（实测通过；无单独 token，bot_id+secret 直连鉴权）。
- 心跳：每 ~25–30s 发 `{"cmd":"ping","headers":{"req_id":"..."}}`。
- 约束（官方+实测）：一个机器人**同一时间只有一条有效长连接，新连接踢掉旧的**；接收模式**长连接 / 回调二选一**。→ 用**专用机器人**，与 wecom-cli 那个（aib3K82…）解耦。
- 凭据放 ECS 运行时 env：`WECOM_COMPANY_B_GROUPBOT_ID` / `WECOM_COMPANY_B_GROUPBOT_SECRET`（**不入 git**）。bot 的实际值在本机由用户提供，已实测有效。

### 0.2 @消息结构（实测样本）
```json
{"cmd":"aibot_msg_callback","headers":{"req_id":"..."},"body":{
  "msgid":"e6fdbf74...",                 // 幂等去重键
  "aibotid":"aib...",
  "chatid":"wrS7aGNQAApqNLsMLS_7RurTthP1vi5A",  // 群唯一键（机器人靠它区分群）
  "chattype":"group",
  "from":{"userid":"WangHao"},            // 明文 userid（无需解密）
  "msgtype":"text",
  "text":{"content":"hello@项目开发管理助手 "},
  "response_url":"https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=...", // 1h 内可回复
  "quote":{"msgtype":"image","image":{"url":"https://...myqcloud.com/..."}} // 引用消息；图片可读到URL
}}
```

### 0.3 能力边界（实测，三轮一致，**不要再设计依赖这些做不到的**）
| 能力 | 结论 |
|---|---|
| 群里**@机器人**的纯文本消息 | ✅ 收到、`text.content` 可读 |
| 引用**图片**消息@机器人 | ✅ `quote.image.url` 可读（可下载） |
| 引用**智能表格自动化卡片**@机器人 | ❌ `quote` = `[该消息类型暂不能展示]`（两轮确认） |
| 自动化**直接@机器人** | ❌ 机器人收不到（0 帧） |
| 共享历史聊天记录 / 加机器人"附带N条记录" | ❌ 不推送 |
| 非@消息 / 成员增减 / 入群事件 | ❌ 不推送 |
| 群名称 | ❌ 回调无群名；`appchat/get` 实测 `86008 created by other agent`（群由智能表格 agent 建，AGI-达无权查） |

**推论**：① 只能拿到"@机器人的纯文本/图片"；② 群名我们读不到，但**用户会把群名复制成纯文本@机器人发出**，群名里已由自动化嵌入**审批单编号**——bot 从这段文本里**自动识别审批单编号**即可完成绑定（详见 3.2）。

### 0.4 需求表 & 审批（已有，复用）
- 「配色&样品需求单」docid=`dc45aa…WyWLA`，sheet「配色&样品需求单」(sheet_id 运行时按标题解析)。已有「关联」列 `f9IbsT`（双向关联类型）可串子表。
- **需求唯一码 = 「审批单编号」列 `f78H1T`**（值如 `202603200003`，与「审批链接」`fOFLD7` 里的 `sp_no` 同值）。以它建索引 `审批单编号 → record_id`，供绑定时匹配；索引由 `get_records` 读该表构建并随同步刷新。
- 审批内容可读：`oa/getapprovaldetail(sp_no)` + `media/get(file_id)`（图片回填管线在用）。
- 图片写智能表格：`wedoc/image_upload`(base64→wdcdn带尺寸) + `update_records` value `[{image_url,title}]`（**勿带 width/height，会 2022014**）。见 [[wecom-smartsheet-image-write-wdcdn]]。

## 1. 架构与组件（doc-sync-worker 内新增；backend-api 仅按需加只读查询）

```
[企微群 @机器人]──长连接──▶ group_bot_listener(常驻,断线重连)
                              │ 解析 aibot_msg_callback
                              ├─ #绑定 指令 ─▶ group_record_map (chatid↔需求行)
                              └─ 普通@消息 ──▶ group_messages 入库(去重/媒体下载)
                                                   │
                          (Phase1 人工标节点 / Phase2 LLM 自动)
                                                   ▼
                                   关联子表「研发过程记录」(update/add_records)
```

- `app/providers/wecom_groupbot.py`：`WeComGroupBotClient`——封装长连接（subscribe/ping/recv 循环、断线重连退避）、`reply(response_url, text)`（被动回复）或 `aibot_send_msg`（主动，限 30/min·1000/h）。
- `app/pipelines/group_message_listener.py`：常驻消费循环，解析回调 → 分派（绑定指令 / 入库）。挂进 worker（独立线程/进程，不阻塞现有同步周期）。
- `app/pipelines/rnd_record_writer.py`：把"已标记节点"的消息写入关联子表。
- store 扩展（`app/storage/postgres.py`）：绑定表、消息表读写。
- backend-api：可选 `GET /v1/ops/group-messages`（运维查看），非必需，Phase1 可省。

## 2. 数据模型（幂等迁移 db/migrations/）

`0021_group_record_map.sql`
```
group_record_map(
  id serial pk, provider text, env_profile text,
  chatid text unique,                 -- 群唯一键
  external_doc_id text, sheet_title text, record_id text,  -- 绑定到的需求行
  requirement_key text,               -- 绑定时用的编号(如样品编号/cskg)
  bound_by text, bound_at timestamptz, updated_at timestamptz)
```

`0022_group_messages.sql`
```
group_messages(
  id serial pk, msgid text unique,    -- 幂等去重
  chatid text, from_userid text, msgtype text,
  text_content text, quote_json jsonb, media_paths jsonb,
  record_id text,                     -- 归属需求(绑定后回填;未绑定为空)
  is_node boolean default false, node_category text, node_summary text,
  ts timestamptz, raw_json jsonb, created_at timestamptz)
index(chatid), index(record_id)
```

`研发过程记录` 子表：**在「配色&样品需求单」同一文档内新建**（`smartsheet_add_sheet`+`add_fields`，或 wecom-cli 手建一次）。字段：时间(DATE_TIME)、发言人(TEXT)、节点类型(SINGLE_SELECT)、内容(TEXT)、图片(IMAGE)、关联需求(双向关联→需求表)。一节点一行。

## 3. 关键流程

### 3.1 捕获（常驻）
1. worker 启动 group_message_listener：连 WSS → `aibot_subscribe`(env 凭据) → 循环 recv + 25s ping。
2. **断线重连**：recv 异常/连接关闭 → 指数退避(1s→…≤30s)重连；重连后重新 subscribe。日志记录每次断开原因。
3. 收到 `aibot_msg_callback`：先按 `msgid` 幂等（已存在则跳过）。

### 3.2 绑定（复制群名 → 自动识别审批单编号）
用户把**自动生成的群名**（自动化已将审批单编号嵌入其中）复制出来，在群里@机器人发出（纯文本，bot 可读）。bot 从该文本自动识别审批单编号并绑定：
1. 从消息文本**提取候选码**（数字串/已知模式，宽松提取，不预设格式）。
2. 逐个在「配色&样品需求单」的 `审批单编号(f78H1T)` / `审批链接 sp_no` 索引中**比对真实值**——能命中的才是有效编号（用真实数据校验，避免误判）。
3. 结果分支：
   - 命中**唯一** → 写 `group_record_map(chatid→record_id, requirement_key=审批单编号)` → `response_url` 回"✅ 已关联 <审批单编号/需求>"。
   - **0 命中** → 回"未在群名中识别到有效审批单编号，请确认已复制完整群名"。
   - **多命中** → 回候选列表让用户确认。
- 不强制 `#绑定` 关键词（自动识别）；但兼容显式 `#绑定 <编号>`。
- **未绑定群第一次被@且未识别到编号** → 回引导"本群尚未关联需求，请把群名复制发我（含审批单编号）完成关联"。

### 3.3 普通@消息入库
- 查 `group_record_map[chatid]` 得 record_id（可能为空=未绑定，仍入库，record_id 留空，绑定后可回填）。
- text → text_content；image（含 `quote.image.url`）→ 下载存盘、路径入 media_paths；quote 原样存 quote_json。
- 入 `group_messages`。

### 3.4 标节点 → 写子表
- **Phase 1（人工）**：默认 `is_node=false` 全量留存；人工标节点两种方式（择一实现，建议指令）：① 在群里 `@机器人 #节点 类型 摘要`（机器人把该条/引用条标为节点）；② 运维在 DB/简单页勾选。标记后由 `rnd_record_writer` 写入「研发过程记录」子表（含图片）。
- **Phase 2（LLM，后续）**：对新入库@消息用现有 Claude/ChatGPT 栈判定 is_node + node_category + node_summary，人工复核后写子表。Phase 1 不实现 LLM。

### 3.5 审批内容带入（可选，复用现有）
绑定时/按需，用需求行的 `审批链接`(sp_no) 调 `getapprovaldetail` 拉审批摘要+附件，作为子表的"起始节点"。

## 4. 错误处理 / 边界
- 长连接：断线自动重连；连续失败告警日志；单条消息处理 try/except 不影响循环。
- 幂等：msgid 唯一；重连后可能重推，靠 msgid 去重。
- 媒体下载失败：记录占位、不阻塞。
- 绑定冲突：同一 chatid 重复绑定=更新（覆盖）。
- 限频：主动回复走 response_url 优先（不计主动发限额）。
- 凭据缺失：env 无 GROUPBOT 凭据 → listener 不启动并告警，不影响其它同步。

## 5. 保留项（不得破坏）
现有"审批附件→「配色&样品需求单」图片列"回填管线（`backfill_smartsheet_images.py` 等）**完全不动**。本功能为独立新增。见 [[wecom-smartsheet-image-write-wdcdn]]。

## 6. 测试（root tests/；网络全 mock）
- 回调解析：text / image+quote / 绑定指令 / 节点指令。
- msgid 去重；未绑定群入库 record_id 空；绑定后归属。
- 群名文本提取审批单编号 + 比对需求表索引（0/1/N 命中分支）+ 写 map。
- 子表写入 payload（图片走 wedoc/image_upload）。
- 断线重连退避逻辑（mock 连接）。
- 参考 `tests/test_wecom_image_backfill.py`、`tests/test_doc_sync_worker.py`。

## 7. Phase 0（已完成）
长连接打通 + 收到真实@消息（text 与 image+quote 均验证）+ 能力边界全部摸清。无遗留未知。

## 8. 一次性人工准备（用户侧）
- 后台把专用机器人「项目开发管理助手」API 模式设为**长连接**（已配，实测可订阅）。
- 每个需求群：把机器人拉进群 + **复制群名@机器人发一次**（群名含审批单编号，bot 自动识别绑定）。
- 自动化建群规则里确保**审批单编号被嵌入群名**。
- 在「配色&样品需求单」文档内建「研发过程记录」子表（或由实现脚本建一次）。

## 9. 风险 / 未决
- 主动发消息限频（30/min·1000/h）——回复优先用 response_url。
- userid 仅 `from.userid`（如 WangHao），转真实姓名需通讯录映射（wecomcli-contact / 通讯录API），Phase1 可先存 userid。
- Phase2 LLM 归类准确率需人工复核闭环。
