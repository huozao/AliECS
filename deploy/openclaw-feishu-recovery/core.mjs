/**
 * OpenClaw 2026.6.5 微信恢复原生飞书卡片固定命令内核（不走 LLM、不走 openclaw-bridge）。
 *
 * 契约对齐：
 * - 命令名：/wechat_recovery
 * - 支持子命令：
 *   1) /wechat_recovery test-card [variant] —— 无副作用自检，不写入中枢状态
 *   2) /wechat_recovery choose <decision_key> <option_key> —— 转发固定 JSON 至中枢
 * - 可信身份与群来源：
 *   固定命令使用 OpenClaw 命令上下文，官方飞书回调使用事件 operator.open_id 与 conversationId，
 *   严禁从 args 读取或由用户透传。
 */

export const WECHAT_RECOVERY_COMMAND_NAME = "wechat_recovery";
export const WECHAT_RECOVERY_INTERACTIVE_NAMESPACE = "wechat-recovery";
export const WECHAT_RECOVERY_DECISION_KEY_RE =
  /^(?:wechat|telegram)-login:(?!test:)([A-Za-z0-9._-]+):([A-Za-z0-9._-]+)$/;
export const WECHAT_RECOVERY_ALLOWED_OPTIONS = new Set(["regen", "skip"]);
export const SAFE_ID_RE = /^[A-Za-z0-9._-]+$/;
export const SAFE_TEST_VARIANT_RE = /^[a-z0-9_-]{1,32}$/;
export const DEFAULT_TIMEOUT_MS = 5000;
const CALLBACK_TIMEOUT_MS = 1800;

export const OPTION_LABELS = Object.freeze({
  regen: "重新生成二维码",
  skip: "暂不处理",
});

function splitListValues(raw) {
  if (Array.isArray(raw)) {
    return raw
      .map((item) => (typeof item === "string" ? item.trim() : ""))
      .filter(Boolean);
  }
  if (typeof raw === "string") {
    return raw
      .split(/[\s,;]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return [];
}

function normalizeHttpBaseUrl(raw) {
  const value = typeof raw === "string" ? raw.trim().replace(/\/+$/, "") : "";
  if (!value) {
    return "";
  }
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return "";
    }
    return `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, "");
  } catch {
    return "";
  }
}

function normalizeOperatorAllowlistEntry(raw) {
  return raw.replace(/^(?:feishu|lark):/i, "").trim();
}

function normalizeChatAllowlistEntry(raw) {
  return raw.replace(/^chat:/i, "").trim();
}

export function normalizePluginConfig(pluginConfig = {}, env = process.env) {
  const cfg = pluginConfig && typeof pluginConfig === "object" ? pluginConfig : {};
  const aliecsBaseUrl = normalizeHttpBaseUrl(
    cfg.aliecsBaseUrl ?? env.NOTIFY_FEISHU_RECOVERY_BASE_URL ?? env.ALIECS_NOTIFY_BASE_URL ?? "",
  );
  const actionToken = String(
    cfg.actionToken ?? env.NOTIFY_FEISHU_RECOVERY_GATEWAY_TOKEN ?? "",
  ).trim();
  const allowedChatIds = Array.from(
    new Set(
      splitListValues(
        cfg.allowedChatIds !== undefined
          ? cfg.allowedChatIds
          : env.NOTIFY_FEISHU_RECOVERY_CHAT_ID ?? "",
      )
        .map(normalizeChatAllowlistEntry)
        .filter((v) => SAFE_ID_RE.test(v)),
    ),
  );
  const allowedOperators = Array.from(
    new Set(
      splitListValues(
        cfg.allowedOperators !== undefined
          ? cfg.allowedOperators
          : env.NOTIFY_FEISHU_RECOVERY_OPERATORS ?? "",
      )
        .map(normalizeOperatorAllowlistEntry)
        .filter((v) => SAFE_ID_RE.test(v)),
    ),
  );
  const rawTimeout = cfg.timeoutMs;
  const parsedTimeout =
    typeof rawTimeout === "number" && Number.isFinite(rawTimeout)
      ? Math.trunc(rawTimeout)
      : DEFAULT_TIMEOUT_MS;
  const timeoutMs = Math.max(500, Math.min(30000, parsedTimeout));

  return {
    aliecsBaseUrl,
    actionToken,
    allowedChatIds,
    allowedOperators,
    timeoutMs,
  };
}

export function extractTrustedFeishuContext(ctx = {}) {
  const channel = String(ctx.channelId || ctx.channel || "").trim().toLowerCase();
  if (channel !== "feishu") {
    return {
      ok: false,
      code: "unsupported_channel",
      reason: "仅支持飞书渠道上下文触发该命令。",
    };
  }

  const senderIdRaw = typeof ctx.senderId === "string" ? ctx.senderId.trim() : "";
  const fromRaw = typeof ctx.from === "string" ? ctx.from.trim() : "";
  let operatorFromPrefix = "";
  if (fromRaw) {
    const match = fromRaw.match(/^(?:feishu|lark):(.+)$/i);
    operatorFromPrefix = match ? match[1].trim() : fromRaw;
  }

  let operatorOpenId = senderIdRaw;
  if (operatorOpenId && operatorFromPrefix && operatorOpenId !== operatorFromPrefix) {
    return {
      ok: false,
      code: "operator_mismatch",
      reason: "可信发送者标识（senderId 与 from）不一致，已拒绝执行。",
    };
  }
  if (!operatorOpenId) {
    operatorOpenId = operatorFromPrefix;
  }
  if (!operatorOpenId || !SAFE_ID_RE.test(operatorOpenId)) {
    return {
      ok: false,
      code: "missing_operator",
      reason: "缺少可信操作者 open_id，已拒绝执行。",
    };
  }

  const toRaw = typeof ctx.to === "string" ? ctx.to.trim() : "";
  if (!toRaw) {
    return {
      ok: false,
      code: "missing_chat",
      reason: "缺少可信会话目标（ctx.to），已拒绝执行。",
    };
  }
  if (/^user:/i.test(toRaw)) {
    return {
      ok: false,
      code: "p2p_context",
      reason:
        "当前回调落在私聊/p2p 上下文（ctx.to 为 user:...），无法确认目标飞书群 chat_id，已按 fail-closed 拒绝。",
    };
  }
  const chatMatch = toRaw.match(/^chat:(.+)$/i);
  const chatId = chatMatch ? chatMatch[1].trim() : "";
  if (!chatId || !SAFE_ID_RE.test(chatId)) {
    return {
      ok: false,
      code: "invalid_chat",
      reason: "无法从可信 ctx.to 解析有效的飞书群 chat_id（chat:<oc_id>），已拒绝执行。",
    };
  }

  return {
    ok: true,
    channel: "feishu",
    operatorOpenId,
    chatId,
  };
}

export function parseRecoveryCommand(rawArgs = "") {
  const trimmed = typeof rawArgs === "string" ? rawArgs.trim() : "";
  if (!trimmed) {
    return { ok: false, kind: "usage" };
  }
  const tokens = trimmed.split(/\s+/).filter(Boolean);
  if (tokens[0] === "test-card") {
    if (tokens.length > 2) {
      return { ok: false, kind: "invalid_args" };
    }
    const variant = (tokens[1] ?? "default").toLowerCase();
    if (!SAFE_TEST_VARIANT_RE.test(variant)) {
      return { ok: false, kind: "invalid_args" };
    }
    return { ok: true, action: "test-card", variant };
  }

  if (tokens[0] === "choose") {
    if (tokens.length !== 3) {
      return { ok: false, kind: "invalid_args" };
    }
    const decisionKey = tokens[1];
    const optionKey = tokens[2];
    if (!WECHAT_RECOVERY_DECISION_KEY_RE.test(decisionKey)) {
      return { ok: false, kind: "invalid_decision_key" };
    }
    if (!WECHAT_RECOVERY_ALLOWED_OPTIONS.has(optionKey)) {
      return { ok: false, kind: "invalid_option_key" };
    }
    return {
      ok: true,
      action: "choose",
      decisionKey,
      optionKey,
    };
  }

  return { ok: false, kind: "unsupported_subcommand" };
}

function sanitizeRemoteDetail(detail) {
  if (typeof detail !== "string") {
    return "";
  }
  return detail.replace(/[\r\n\t]+/g, " ").trim().slice(0, 160);
}

async function executeRecoveryAction(ctx, options) {
  const parsedCmd = parseRecoveryCommand(ctx?.args ?? "");
  if (!parsedCmd.ok) {
    return {
      text: [
        "⛔ 仅接受固定微信恢复卡片指令，不会转交模型或执行任意命令。",
        "受支持用法：",
        "- /wechat_recovery test-card [variant]",
        "- /wechat_recovery choose wechat-login:<掉线ID>:<尝试号> <regen|skip>",
      ].join("\n"),
      toast: { type: "error", content: "无法识别此恢复操作。" },
    };
  }

  const config = normalizePluginConfig(options.pluginConfig, options.env);
  const trusted = extractTrustedFeishuContext(ctx);
  const probeOnly = options.pluginConfig?.probeOnly === true;

  if (parsedCmd.action === "test-card") {
    const channelReady = trusted.ok;
    const chatAllowed = channelReady && config.allowedChatIds.includes(trusted.chatId);
    const operatorAllowed =
      channelReady && config.allowedOperators.includes(trusted.operatorOpenId);
    return {
      text: [
        `🧪 微信恢复无副作用测试卡回执（variant=${parsedCmd.variant}）`,
        "- 执行路径：OpenClaw registerCommand 直达（未经过 LLM 与 openclaw-bridge）",
        "- 业务副作用：无（未调用 AliECS feishu-choice，未修改决策状态）",
        `- 可信上下文：${channelReady ? "已识别飞书操作人与群" : `未就绪（${trusted.reason}）`}`,
        `- 配置状态：baseUrl=${config.aliecsBaseUrl ? "已配置" : "未配置"}，token=${config.actionToken ? "已配置" : "未配置"}，群白名单命中=${chatAllowed}，操作者白名单命中=${operatorAllowed}`,
      ].join("\n"),
      toast: chatAllowed && (probeOnly || operatorAllowed)
        ? { type: "success", content: "回调测试成功，未发起二维码任务。" }
        : { type: "error", content: "当前群或操作者无权执行恢复操作。" },
    };
  }

  if (probeOnly) {
    return {
      text: "⛔ 当前只启用了无副作用回调测试。",
      toast: { type: "error", content: "当前只启用了回调测试，请使用网页确认。" },
    };
  }

  if (!trusted.ok) {
    return {
      text: `⛔ ${trusted.reason}`,
      toast: { type: "error", content: "无法确认飞书操作者或群身份。" },
    };
  }

  if (
    !config.aliecsBaseUrl ||
    !config.actionToken ||
    config.allowedChatIds.length === 0 ||
    config.allowedOperators.length === 0
  ) {
    return {
      text: "⛔ 微信恢复插件配置不完整（需配置 aliecsBaseUrl、actionToken、群白名单与操作者白名单），已按 fail-closed 拒绝。请改用网页确认页。",
      toast: { type: "error", content: "恢复服务配置未就绪，请使用网页确认。" },
    };
  }

  if (!config.allowedChatIds.includes(trusted.chatId)) {
    return {
      text: `⛔ 当前群 ${trusted.chatId} 不在允许的恢复群白名单内，已拒绝提交。`,
      toast: { type: "error", content: "当前群无权执行恢复操作。" },
    };
  }

  if (!config.allowedOperators.includes(trusted.operatorOpenId)) {
    return {
      text: `⛔ 当前操作者 ${trusted.operatorOpenId} 不在授权操作者白名单内，已拒绝提交。`,
      toast: { type: "error", content: "当前操作者无权执行恢复操作。" },
    };
  }

  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const endpoint = `${config.aliecsBaseUrl}/v1/internal/notify/decisions/feishu-choice`;
  const requestBody = {
    decision_key: parsedCmd.decisionKey,
    option_key: parsedCmd.optionKey,
    operator_open_id: trusted.operatorOpenId,
    chat_id: trusted.chatId,
  };

  let response;
  try {
    response = await fetchImpl(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Notify-Feishu-Action-Token": config.actionToken,
      },
      body: JSON.stringify(requestBody),
      signal: AbortSignal.timeout(Math.min(config.timeoutMs, options.maxTimeoutMs ?? config.timeoutMs)),
      redirect: "error",
    });
  } catch (err) {
    const errName = err instanceof Error ? err.name : "RequestError";
    return {
      text: [
        `⚠️ 中枢提交结果未知（${errName}），系统不会自动重放或生成第二张二维码卡片。`,
        `请核对决策 ${parsedCmd.decisionKey} 当前状态，或改用网页确认页继续处理。`,
      ].join("\n"),
      toast: { type: "info", content: "受理结果未确认，请先查看状态或打开网页确认。" },
    };
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }

  if (
    response.status === 200 &&
    data &&
    data.ok === true &&
    data.decision_key === parsedCmd.decisionKey &&
    data.option_key === parsedCmd.optionKey
  ) {
    const label = OPTION_LABELS[parsedCmd.optionKey] ?? parsedCmd.optionKey;
    if (data.newly_recorded === false) {
      return {
        text: "该选择已记录，请查看最新通知。",
        toast: { type: "info", content: "该选择已记录，请查看最新通知。" },
      };
    }
    return {
      text: [
        `✅ 已记录微信恢复选择：${label}（${parsedCmd.optionKey}）`,
        `决策键：${parsedCmd.decisionKey}`,
        "后续处理由中枢异步执行或保持当前状态；本回调不会再生成第二张卡片。",
      ].join("\n"),
      recoveryCard: recoveryStatusCard(parsedCmd.decisionKey, parsedCmd.optionKey),
      toast: {
        type: "success",
        content: parsedCmd.optionKey === "regen" ? "已受理，正在生成二维码。" : "已记录，暂不处理。",
      },
    };
  }

  if (response.status >= 400 && response.status < 500) {
    const detail = sanitizeRemoteDetail(data?.detail);
    return {
      text: [
        `❌ 中枢拒绝本次操作（HTTP ${response.status}${detail ? `：${detail}` : ""}）。`,
        `决策键：${parsedCmd.decisionKey}`,
        "如仍需确认，请在群内或网页确认页核对最新状态。",
      ].join("\n"),
      toast: { type: "error", content: "中枢拒绝了本次操作，请打开网页核对状态。" },
    };
  }

  return {
    text: [
      `⚠️ 中枢返回未确认响应（HTTP ${response.status}），系统不会自动重试或生成第二张卡片。`,
      `请先核对决策 ${parsedCmd.decisionKey} 状态，或通过网页确认页完成操作。`,
    ].join("\n"),
    toast: { type: "info", content: "受理结果未确认，请先查看状态或打开网页确认。" },
  };
}

export async function handleRecoveryCommand(ctx, options = {}) {
  const { text } = await executeRecoveryAction(ctx, options);
  return { text };
}

function recoveryStatusCard(decisionKey, optionKey) {
  const service = decisionKey.startsWith("telegram-login:") ? "Telegram" : "微信";
  const status = optionKey === "regen" ? "正在生成新二维码" : "已记录，暂不处理";
  return {
    schema: "2.0",
    config: { update_multi: true },
    header: { title: { tag: "plain_text", content: `${service} 登录恢复` } },
    body: { elements: [{ tag: "markdown", content: status }] },
  };
}

export async function handleRecoveryInteractive(ctx, options = {}) {
  // The official channel may fall back to user_id for senderId. The recovery allowlist
  // uses app-scoped open_id, so require the actual operator field in the SDK event.
  const operatorOpenId = ctx?.rawEvent?.operator?.open_id;
  const openId = typeof operatorOpenId === "string" ? operatorOpenId.trim() : undefined;
  const { toast, recoveryCard } = await executeRecoveryAction(
    {
      channel: ctx?.channel,
      senderId: openId === ctx?.senderId ? openId : undefined,
      to: ctx?.conversationId ? `chat:${ctx.conversationId}` : undefined,
      args: ctx?.payload,
    },
    { ...options, maxTimeoutMs: CALLBACK_TIMEOUT_MS },
  );
  return recoveryCard ? { toast, card: { type: "raw", data: recoveryCard } } : { toast };
}

export function buildSideEffectFreeTestCard(options = {}) {
  const variant = SAFE_TEST_VARIANT_RE.test(String(options.variant ?? "default").toLowerCase())
    ? String(options.variant ?? "default").toLowerCase()
    : "default";
  return {
    schema: "2.0",
    config: { width_mode: "fill", update_multi: true },
    header: {
      title: { tag: "plain_text", content: "微信恢复原生卡片离线测试卡（无副作用）" },
      subtitle: {
        tag: "plain_text",
        content: "仅验证飞书回调与可信上下文，不改状态、不发真实 QR",
      },
      template: "wathet",
      text_tag_list: [
        {
          tag: "text_tag",
          text: { tag: "plain_text", content: "无副作用测试" },
          color: "turquoise",
        },
      ],
    },
    body: {
      direction: "vertical",
      elements: [
        {
          tag: "markdown",
          content:
            "点击下方按钮只会触发 `/wechat_recovery test-card`，" +
            "在 OpenClaw 2026.6.5 插件层回执可信群与操作者诊断信息，不请求 `feishu-choice` 也不生成第二张卡片。",
        },
        {
          tag: "column_set",
          flex_mode: "flow",
          horizontal_spacing: "8px",
          columns: [
            {
              tag: "column",
              width: "auto",
              elements: [
                {
                  tag: "button",
                  text: { tag: "plain_text", content: "测试原生回调（无副作用）" },
                  type: "primary",
                  width: "default",
                  behaviors: [
                    {
                      type: "callback",
                      value: {
                        action: `${WECHAT_RECOVERY_INTERACTIVE_NAMESPACE}:test-card ${variant}`,
                        oc: "ocf1",
                        k: "quick",
                        a: "wechat-recovery:test-card",
                        q: `/${WECHAT_RECOVERY_COMMAND_NAME} test-card ${variant}`,
                        c: { t: "group" },
                      },
                    },
                  ],
                },
              ],
            },
          ],
        },
        {
          tag: "markdown",
          text_size: "notation",
          content: "wechat-digest · test-card（示例卡片，严禁向生产群直接发送或写入真实凭据）",
        },
      ],
    },
  };
}
