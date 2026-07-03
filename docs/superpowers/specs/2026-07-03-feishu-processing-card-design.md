# 飞书"处理中"单卡片方案设计（劝阻连续提问）

日期：2026-07-03
状态：已实现（bridge 单文件，特性开关默认关暗部署；待镜像构建 + 手动 cutover + 真机灰度）
范围：`AliECS/deploy/openclaw-bridge/openclaw_bridge.py`（bridge），仅飞书渠道

## 1. 目标与非目标

**要解决的真实问题**：用户在没看到回复时，会连续追问多个（往往不同的）问题，导致串行堆积、体验割裂。根因是"发出问题后一段时间（通常 20–60s）没有明确反馈"。

**目标（劝阻/预防）**：每条问题**一进来就立刻回一张卡片**告知"正在处理，请稍候再问"，让用户主动停手；答案就绪后**把同一张卡片就地更新成最终答案**——始终只有一张卡，不产生"提示 + 答案"两条消息（避免"乱"）。

**非目标**：
- 不做"把多个问题合并成一次上下文作答"（那是用户明确未选的"吸收/合并"路线）。
- 不做逐字流式（WebDock 一次性返回全文，用不上 Card Kit 流式）。
- 不改企业微信渠道（无飞书卡片语义）。
- 不修 batching 的文本吞并坑（见 §7，独立议题）。

## 2. 已核实的事实基线（代码级）

| 事实 | 证据 |
|---|---|
| 回复=引用回复，逐条锚定用户消息 | `feishu_send_interactive_message` → `/im/v1/messages/{id}/reply`（`openclaw_bridge.py:1094`） |
| 多条消息 <2s → batching 合并成一次调用，非领队返 `NO_REPLY` | `maybe_batch_request`（`:2013`），窗口 `OPENCLAW_BRIDGE_BATCH_SECONDS=2.0` |
| 多条消息 >2s → 各自成请求，webdock 侧 lane 锁串行排队，各自作答 | `lane_scheduler.py:173` `async with lane_lock`（await 排队，不拒绝） |
| 并发不返 429 | `ErrorCode.BUSY` 在 webdock 源码从不抛出（全局仅定义+映射） |
| 飞书卡片可发出后更新 | 插件 `im.message.patch`=`PATCH /im/v1/messages/{id}`（`send-B3kteMF8.js:1099`）；另有 Card Kit 流式接口（本方案不用） |
| "敲键盘⌨"是插件加的 `Typing` 表情回复，受配置 `typingIndicator`（默认 true）控制，无法带文字 | 插件 `typing.ts` `TYPING_EMOJI="Typing"`（`monitor.account:1201`） |

## 3. 总体设计：单张演进卡片

一条用户问题的生命周期：

```
用户: Q1
  └─(bridge 收到, batch flush 后, 调 webdock 前)
      bridge 发【占位卡】"⏳ 正在处理你的问题…" ──引用回复到 Q1── 记下 placeholder_msg_id
  └─ call_webdock (20–60s, 期间 webdock 串行排队)
  └─ 答案就绪:
      bridge PATCH placeholder_msg_id ← 用最终答案卡片内容(文本/图片/footer)
      build_reply 返回 NO_REPLY (OpenClaw 不再另发)
最终: 聊天里只有 Q1 + 一张从"正在处理"变成"答案"的卡片
```

占位卡文案随"是否追问"分两级（见 §5）。答案就绪采用**替换**（非续写）：占位文字消失、卡片整体变成答案，最干净。

## 4. 控制流（`build_reply` 内，飞书且开关开启时）

插入点：`maybe_batch_request` 已返回真实 body（领队）之后、`call_webdock` 之前（`openclaw_bridge.py:2444–2456` 区间）。此点每"一次真正打 ChatGPT 的轮次"有且仅经过一次（非领队 / `/新对话` / "仅记录" 都在更上游 `NO_REPLY` 返回，天然排除）。

新增模块级状态（复刻现有 `_pending_batches` 加锁范式）：
```python
_inflight_counts: dict[str, int] = {}   # lane_key -> 正在飞的 webdock 调用数
_inflight_lock = Lock()
```

伪代码：
```python
lane_key = lane_batch_key(write_details["metadata"])
placeholder_id = None
with _inflight_lock:
    prior = _inflight_counts.get(lane_key, 0)
    _inflight_counts[lane_key] = prior + 1
try:
    if channel == "feishu" and notice_enabled and has_feishu_creds:
        text = REMIND_TEXT if prior > 0 else ACK_TEXT
        placeholder_id = send_processing_card(write_details, text)  # 引用回复用户消息, 返回 message_id; 失败返 None
        write_details["feishu_placeholder_msg_id"] = placeholder_id

    result = call_webdock(batched_body)                 # 照常; webdock 侧自动串行排队
    reply, meta = unpack_webdock_result(result)
    write_details 更新 footer/metadata

    reply = deliver_feishu_files(reply, write_details)
    reply = deliver_feishu_media(reply, write_details)  # 见 §6: 出站改为"有占位则 patch, 否则新发"
    reply = deliver_feishu_text_card(reply, write_details)
    return reply
finally:
    with _inflight_lock:
        n = _inflight_counts.get(lane_key, 1) - 1
        _inflight_counts.pop(lane_key, None) if n <= 0 else _inflight_counts.__setitem__(lane_key, n)
```

## 5. 占位卡文案（两级）

- `prior == 0`（本轮第一条）→ **ACK_TEXT**：`⏳ 正在处理你的问题（通常 20–60 秒），收到回复后再继续提问哦～`
- `prior > 0`（上一条还在飞就又问了）→ **REMIND_TEXT**：`⚠️ 上一条还在处理中，这条已排队；请等回复后再问，连续提问会让每条都变慢。`

追问的第二条**同样是"占位→答案"单卡**（只是占位文字不同），因此不产生额外气泡，`乱`的问题被根治。`_inflight_counts` 仅用于**选占位文案**，不再旁路多发消息。

## 6. 出站投递改造（关键 seam）

现状：`deliver_feishu_media` / `deliver_feishu_text_card` 构建卡片后调 `feishu_send_interactive_message(details, feishu_message_id(details), card, token)`（新发一条引用回复），返回 `NO_REPLY`。

改造：抽出统一投递助手
```python
def feishu_put_card(details, card, token):
    pid = details.get("feishu_placeholder_msg_id")
    if pid:
        try:
            feishu_patch_message(pid, card, token)   # PATCH /im/v1/messages/{pid} {"content": card_json}
            return
        except Exception as e:
            log(...)  # patch 失败则降级新发, 保证答案不丢
    feishu_send_interactive_message(details, feishu_message_id(details), card, token)
```
两个 `deliver_feishu_*` 改为调用 `feishu_put_card`。卡片构建逻辑（图片上传、有序段落、footer）**完全复用不变**，只改"卡片发去哪"。

`feishu_patch_message` 为新增薄封装（bridge 已有 `feishu_post_json`，增一个 method=PATCH 版本即可）。

占位卡构建：复用 `build_feishu_card`，并置 `config.update_multi=true`（飞书可更新卡片要求），否则后续 patch 可能被拒。

## 7. 失败与降级矩阵

| 情况 | 处置 | 结果 |
|---|---|---|
| 无飞书凭据 / token 取失败 | 不发占位卡（`placeholder_id=None`），走现状旧路径 | 与今天一致，答案照发 |
| 占位卡发送失败 | `placeholder_id=None`，出站走"新发引用回复" | 无占位，答案照发（退回今天行为） |
| 占位发成功但 PATCH 失败 | `feishu_put_card` 捕获后降级"新发引用回复" | 答案不丢；用户看到占位 + 答案两条（罕见降级，可接受） |
| `call_webdock` 抛异常 | `finally` 计数归零；异常分支的诊断消息也经 `feishu_put_card` patch 进占位卡 | 占位卡变成错误提示，不残留"正在处理" |
| WebDock 返回空 / `NO_REPLY` 类 | 若已发占位卡，需 patch 成一句兜底（如"本条无需回复"）或撤回，避免占位永久残留 | 见 §11 待定 |

## 8. 不变量（严谨性）

1. **不丢消息**：占位卡只是"预先占坑"，`call_webdock` 照常执行，追问的第二条仍被 webdock 串行回答 → 与"请等回复"文案自洽。
2. **计数必归零**：`finally` 递减，异常也不泄漏，`_inflight_counts` 不会永久卡死误判 overlap。
3. **一轮一卡**：非领队 / `/新对话` / "仅记录" 在更上游 `NO_REPLY` 返回，进不到本段。
4. **单气泡**：一条问题全程只有一张卡（占位→答案），不产生第二条。
5. **旁路独立**：占位与 patch 都是 bridge 直发飞书 API，不改变 `build_reply` 对 OpenClaw 的返回契约（成功自投递后返回 `NO_REPLY`，与现有 `deliver_feishu_*` 一致）。

## 9. 与 typingIndicator 的关系

占位卡是比 ⌨ 表情**更清晰**的"处理中"信号，二者会有轻微冗余。建议上线后视观感决定是否配置 `typingIndicator:false`（ECS `openclaw.json` 飞书段，零改码，与本方案解耦）。默认保留，不强制。

## 10. 配置开关（默认关，灰度安全）

- `OPENCLAW_BRIDGE_PROCESSING_CARD=0/1`：总开关，默认 0（暗部署）。
- `OPENCLAW_BRIDGE_PROCESSING_ACK_TEXT` / `..._REMIND_TEXT`：两级文案可覆盖。
- 无需冷却参数（不再旁路多发消息，占位卡本身一轮一张）。

## 11. 待定 / 未决

- **孤儿占位（已收窄，基本关闭）**：核心约束——**只对"会真正送去 ChatGPT 的消息"发占位，绝不对"仅记录"类发**。所有"设计上就不回复"的路径（batch 非领队、`/新对话`、"仅记录" `feishu_should_send_chatgpt=false`）都在 `build_reply` 更上游、**发占位之前**就 `return NO_REPLY`（`:2440`），因此不会产生孤儿占位。占位插入点必须严格保持在这些过滤之后（本设计 §4 已如此）。
  - 唯一残留边缘（与"该不该回复"无关）：webdock **本该答却返回空字符串**（软失败/空返回）。处置：出站助手在 reply 为空且已有占位时，patch 成兜底文案（如"本次没有生成内容，请重试"），保证占位不残留。这是一个窄边缘，实现时在 `feishu_put_card` / 出站汇聚点统一兜底即可。
- **占位时机**：当前在 batch flush 后（~2s+），若嫌慢可考虑挪到 `maybe_batch_request` 领队创建处（更早约 2s 发出），但更侵入，v1 先不做。

## 12. 已知限制（明确划到范围外）

**batching 文本吞并坑**（`PendingBatch.merge` `:174`）：2s 内连发时 `self.user_text = details["user_text"]` 是后覆盖前，只有图片累加。→ 2s 内连发两个不同问题，前一个文字被静默丢弃、答案还引用回复到前一条。本设计**不修**（修它=实现"合并"路线，用户未选）；且打字提问通常 >2s 走串行、不触发此坑。记录为独立议题。

## 13. 测试计划（红绿）

- 同 lane 并发两请求：断言第一条占位走 ACK、第二条走 REMIND；两条各自 patch 成各自答案；结束后 `_inflight_counts` 归零。
- `call_webdock` 抛异常：断言计数归零，且占位卡被 patch 成错误提示（不残留"正在处理"）。
- 无凭据 / 占位发送失败 / patch 失败：断言分别降级到旧"新发"路径，答案不丢。
- `NO_REPLY` 三类路径（非领队 / `/新对话` / 仅记录）：不发占位、不计数。
- feishu API mock：断言占位发一次、patch 发一次（正常路径共两次调用）。

## 14. 部署

改 `openclaw_bridge.py` → **bridge 镜像构建 + 手动 cutover**（非自动，见既有约定）。开关默认关 → 暗部署 → 真机开开关验证（含追问、异常、图片答案三场景）→ 稳定后默认开。
