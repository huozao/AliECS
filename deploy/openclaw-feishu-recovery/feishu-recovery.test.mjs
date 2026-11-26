import assert from "node:assert/strict";
import { describe, it } from "node:test";
import plugin from "./index.mjs";

import {
  WECHAT_RECOVERY_COMMAND_NAME,
  buildSideEffectFreeTestCard,
  extractTrustedFeishuContext,
  handleRecoveryCommand,
  normalizePluginConfig,
  parseRecoveryCommand,
} from "./core.mjs";

const BASE_CONFIG = {
  aliecsBaseUrl: "https://notify.example.com",
  actionToken: "example-gateway-token",
  allowedChatIds: ["oc_example_recovery_chat"],
  allowedOperators: ["ou_example_operator_01"],
  timeoutMs: 3000,
};

const VALID_GROUP_CTX = {
  channel: "feishu",
  channelId: "feishu",
  senderId: "ou_example_operator_01",
  from: "feishu:ou_example_operator_01",
  to: "chat:oc_example_recovery_chat",
  isAuthorizedSender: true,
};

describe("feishu-recovery command parser & fail-closed guards", () => {
  it("parses only fixed test-card and choose commands", () => {
    assert.deepEqual(parseRecoveryCommand("test-card"), {
      ok: true,
      action: "test-card",
      variant: "default",
    });
    assert.deepEqual(
      parseRecoveryCommand("choose wechat-login:outage-20261126:2 regen"),
      {
        ok: true,
        action: "choose",
        decisionKey: "wechat-login:outage-20261126:2",
        optionKey: "regen",
      },
    );
    assert.deepEqual(
      parseRecoveryCommand("choose wechat-login:outage-20261126:2 skip"),
      {
        ok: true,
        action: "choose",
        decisionKey: "wechat-login:outage-20261126:2",
        optionKey: "skip",
      },
    );
  });

  it("rejects test decision keys, unknown options, extra args, and arbitrary commands", () => {
    for (const badArgs of [
      "",
      "   ",
      "help",
      "exec rm -rf /",
      "choose wechat-login:test:1700000000 regen",
      "choose wechat-login:outage:1 seen",
      "choose wechat-login:outage:1 regen extra-arg",
      "choose random-key:1 regen",
      "test-card ../../bad",
      "test-card one two",
    ]) {
      const parsed = parseRecoveryCommand(badArgs);
      assert.equal(parsed.ok, false, `expected reject for args=${JSON.stringify(badArgs)}`);
    }
  });

  it("extracts operator_open_id and chat_id strictly from trusted OpenClaw context", () => {
    const trusted = extractTrustedFeishuContext(VALID_GROUP_CTX);
    assert.deepEqual(trusted, {
      ok: true,
      channel: "feishu",
      operatorOpenId: "ou_example_operator_01",
      chatId: "oc_example_recovery_chat",
    });

    assert.equal(
      extractTrustedFeishuContext({ ...VALID_GROUP_CTX, channel: "telegram", channelId: "telegram" }).ok,
      false,
    );
    assert.equal(
      extractTrustedFeishuContext({ ...VALID_GROUP_CTX, to: "user:ou_example_operator_01" }).code,
      "p2p_context",
    );
    assert.equal(
      extractTrustedFeishuContext({ ...VALID_GROUP_CTX, from: "feishu:ou_spoofed" }).code,
      "operator_mismatch",
    );
    assert.equal(
      extractTrustedFeishuContext({ ...VALID_GROUP_CTX, to: "" }).code,
      "missing_chat",
    );
  });

  it("normalizes config from pluginConfig or fallback env fail-closed", () => {
    const emptyCfg = normalizePluginConfig({}, {});
    assert.equal(emptyCfg.aliecsBaseUrl, "");
    assert.equal(emptyCfg.actionToken, "");
    assert.deepEqual(emptyCfg.allowedChatIds, []);
    assert.deepEqual(emptyCfg.allowedOperators, []);

    const envCfg = normalizePluginConfig(
      {},
      {
        NOTIFY_FEISHU_RECOVERY_BASE_URL: "https://notify.example.com/",
        NOTIFY_FEISHU_RECOVERY_GATEWAY_TOKEN: "tok-123",
        NOTIFY_FEISHU_RECOVERY_CHAT_ID: "oc_chat_1, chat:oc_chat_2",
        NOTIFY_FEISHU_RECOVERY_OPERATORS: "feishu:ou_op1 ou_op2",
      },
    );
    assert.equal(envCfg.aliecsBaseUrl, "https://notify.example.com");
    assert.equal(envCfg.actionToken, "tok-123");
    assert.deepEqual(envCfg.allowedChatIds, ["oc_chat_1", "oc_chat_2"]);
    assert.deepEqual(envCfg.allowedOperators, ["ou_op1", "ou_op2"]);
  });
});

describe("feishu-recovery command execution (offline mock HTTP)", () => {
  it("handles test-card without side effects, HTTP calls, or token leakage", async () => {
    let fetchCalled = false;
    const res = await handleRecoveryCommand(
      {
        ...VALID_GROUP_CTX,
        commandBody: `/${WECHAT_RECOVERY_COMMAND_NAME} test-card default`,
        args: "test-card default",
      },
      {
        pluginConfig: BASE_CONFIG,
        fetchImpl: async () => {
          fetchCalled = true;
          throw new Error("should not call fetch for test-card");
        },
      },
    );
    assert.equal(fetchCalled, false);
    assert.equal(res.continueAgent, undefined);
    assert.match(res.text, /无副作用测试卡回执/);
    assert.doesNotMatch(res.text, /example-gateway-token/);
  });

  it("rejects untrusted commands and never passes through to LLM even if user sends prompt text", async () => {
    let fetchCalled = false;
    const res = await handleRecoveryCommand(
      {
        ...VALID_GROUP_CTX,
        args: "请帮我打开浏览器重新登录微信",
      },
      {
        pluginConfig: BASE_CONFIG,
        fetchImpl: async () => {
          fetchCalled = true;
          throw new Error("must not call fetch");
        },
      },
    );
    assert.equal(fetchCalled, false);
    assert.equal(res.continueAgent, undefined);
    assert.match(res.text, /不会转交模型或执行任意命令/);
  });

  it("fails closed on empty allowlists or unauthorized operator/chat without calling AliECS", async () => {
    let fetchCalled = false;
    const failFetch = async () => {
      fetchCalled = true;
      throw new Error("must not reach AliECS");
    };

    const unconfigured = await handleRecoveryCommand(
      { ...VALID_GROUP_CTX, args: "choose wechat-login:out-1:1 regen" },
      { pluginConfig: { ...BASE_CONFIG, allowedOperators: [] }, env: {}, fetchImpl: failFetch },
    );
    assert.match(unconfigured.text, /fail-closed/);

    const wrongUser = await handleRecoveryCommand(
      {
        ...VALID_GROUP_CTX,
        senderId: "ou_attacker",
        from: "feishu:ou_attacker",
        args: "choose wechat-login:out-1:1 regen",
      },
      { pluginConfig: BASE_CONFIG, env: {}, fetchImpl: failFetch },
    );
    assert.match(wrongUser.text, /不在授权操作者白名单/);

    const wrongChat = await handleRecoveryCommand(
      {
        ...VALID_GROUP_CTX,
        to: "chat:oc_other_group",
        args: "choose wechat-login:out-1:1 regen",
      },
      { pluginConfig: BASE_CONFIG, env: {}, fetchImpl: failFetch },
    );
    assert.match(wrongChat.text, /不在允许的恢复群白名单/);

    assert.equal(fetchCalled, false);
  });

  it("forwards exact feishu-choice JSON and header and handles 200 success", async () => {
    const calls = [];
    const res = await handleRecoveryCommand(
      {
        ...VALID_GROUP_CTX,
        commandBody: "/wechat_recovery choose wechat-login:out-99:3 regen",
        // Even if someone tried appending extra tokens, choose rejects !=3 tokens;
        // here args has exact 3 tokens and identity MUST come from VALID_GROUP_CTX.
        args: "choose wechat-login:out-99:3 regen",
      },
      {
        pluginConfig: BASE_CONFIG,
        fetchImpl: async (url, init) => {
          calls.push({ url, init });
          return new Response(
            JSON.stringify({
              ok: true,
              decision_key: "wechat-login:out-99:3",
              option_key: "regen",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        },
      },
    );

    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "https://notify.example.com/v1/internal/notify/decisions/feishu-choice");
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.headers["X-Notify-Feishu-Action-Token"], "example-gateway-token");
    assert.deepEqual(JSON.parse(calls[0].init.body), {
      decision_key: "wechat-login:out-99:3",
      option_key: "regen",
      operator_open_id: "ou_example_operator_01",
      chat_id: "oc_example_recovery_chat",
    });
    assert.equal(res.continueAgent, undefined);
    assert.match(res.text, /✅ 已记录微信恢复选择：重新生成二维码（regen）/);
    assert.match(res.text, /不会再生成第二张卡片/);
  });

  it("handles 4xx rejection and unknown 5xx/network errors without replay or second card", async () => {
    const rejected = await handleRecoveryCommand(
      { ...VALID_GROUP_CTX, args: "choose wechat-login:out-99:3 skip" },
      {
        pluginConfig: BASE_CONFIG,
        fetchImpl: async () =>
          new Response(JSON.stringify({ detail: "decision is no longer current" }), {
            status: 409,
            headers: { "Content-Type": "application/json" },
          }),
      },
    );
    assert.match(rejected.text, /HTTP 409/);
    assert.match(rejected.text, /decision is no longer current/);

    const serverErr = await handleRecoveryCommand(
      { ...VALID_GROUP_CTX, args: "choose wechat-login:out-99:3 regen" },
      {
        pluginConfig: BASE_CONFIG,
        fetchImpl: async () => new Response("upstream timeout", { status: 504 }),
      },
    );
    assert.match(serverErr.text, /未确认响应（HTTP 504）/);
    assert.match(serverErr.text, /不会自动重试或生成第二张卡片/);

    const networkErr = await handleRecoveryCommand(
      { ...VALID_GROUP_CTX, args: "choose wechat-login:out-99:3 regen" },
      {
        pluginConfig: BASE_CONFIG,
        fetchImpl: async () => {
          throw new DOMException("The operation timed out", "TimeoutError");
        },
      },
    );
    assert.match(networkErr.text, /结果未知（TimeoutError）/);
    assert.match(networkErr.text, /不会自动重放或生成第二张二维码卡片/);
  });

  it("builds valid side-effect-free test card JSON 2.0", () => {
    const card = buildSideEffectFreeTestCard();
    assert.equal(card.schema, "2.0");
    const button = card.body.elements[1].columns[0].elements[0];
    assert.equal(button.behaviors[0].type, "callback");
    assert.deepEqual(button.behaviors[0].value, {
      action: "wechat-recovery:test-card default",
      oc: "ocf1",
      k: "quick",
      a: "wechat-recovery:test-card",
      q: "/wechat_recovery test-card default",
      c: { t: "group" },
    });
  });
});

describe("official Feishu interactive callback", () => {
  function registerHandler(pluginConfig = BASE_CONFIG) {
    let registration;
    plugin.register({
      pluginConfig,
      registerCommand() {},
      registerInteractiveHandler(value) {
        registration = value;
      },
    });
    assert.equal(registration.channel, "feishu");
    assert.equal(registration.namespace, "wechat-recovery");
    return registration.handler;
  }

  const callback = {
    channel: "feishu",
    payload: "choose wechat-login:example:1 regen",
    senderId: VALID_GROUP_CTX.senderId,
    conversationId: "oc_example_recovery_chat",
    rawEvent: { operator: { open_id: VALID_GROUP_CTX.senderId } },
  };

  it("returns a success toast only after the authorized choice is recorded", async (t) => {
    const fetch = t.mock.method(globalThis, "fetch", async (_url, init) => {
      assert.deepEqual(JSON.parse(init.body), {
        decision_key: "wechat-login:example:1",
        option_key: "regen",
        operator_open_id: callback.senderId,
        chat_id: callback.conversationId,
      });
      return Response.json({ ok: true, decision_key: "wechat-login:example:1", option_key: "regen" });
    });
    const response = await registerHandler()(callback);
    assert.deepEqual(response.toast, { type: "success", content: "已受理，正在生成二维码。" });
    assert.equal(response.card.type, "raw");
    assert.deepEqual(response.card.data.body.elements, [{ tag: "markdown", content: "正在生成新二维码" }]);
    assert.equal(fetch.mock.callCount(), 1);
  });

  it("separates an unconfirmed submission from permission errors and does not replay", async (t) => {
    const fetch = t.mock.method(globalThis, "fetch", async () => Response.json({}, { status: 502 }));
    const handler = registerHandler();
    assert.equal((await handler(callback)).toast.type, "info");
    assert.equal((await handler({ ...callback, rawEvent: { operator: { user_id: callback.senderId } } })).toast.type, "error");
    assert.equal((await handler({ ...callback, conversationId: "oc_other_group" })).toast.type, "error");
    assert.equal((await handler({ ...callback, payload: "test-card default" })).toast.type, "success");
    assert.equal(fetch.mock.callCount(), 1);
  });

  it("limits probe-only mode to the configured chat and blocks all choices", async (t) => {
    const fetch = t.mock.method(globalThis, "fetch");
    const handler = registerHandler({ probeOnly: true, allowedChatIds: [callback.conversationId] });
    assert.equal((await handler({ ...callback, payload: "test-card default" })).toast.type, "success");
    assert.equal((await handler({ ...callback, payload: "test-card default", conversationId: "oc_other" })).toast.type, "error");
    assert.equal((await handler(callback)).toast.type, "error");
    assert.equal(fetch.mock.callCount(), 0);
  });
});
