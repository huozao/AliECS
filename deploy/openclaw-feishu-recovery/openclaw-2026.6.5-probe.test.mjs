import assert from "node:assert/strict";
import fs from "node:fs";
import moduleApi from "node:module";
import path from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { WECHAT_RECOVERY_COMMAND_NAME, buildSideEffectFreeTestCard } from "./core.mjs";
import pluginEntry from "./index.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OPENCLAW_SRC_DIR = process.env.OPENCLAW_SRC_DIR?.trim() || "";
const OPENCLAW_PKG_DIR = process.env.OPENCLAW_PKG_DIR?.trim() || "";

function loadCardInteractionModuleFromSource(srcRoot) {
  const cardInteractionPath = path.join(srcRoot, "extensions/feishu/src/card-interaction.ts");
  const rawTs = fs.readFileSync(cardInteractionPath, "utf8");
  const stripped = moduleApi.stripTypeScriptTypes(rawTs, {
    mode: "strip",
    sourceUrl: pathToFileURL(cardInteractionPath).href,
  });
  // 仅替换内部纯辅助函数 isRecord 的跨模块引入，完整保留 OpenClaw 2026.6.5 decodeFeishuCardAction 实现
  const patched = stripped.replace(
    /import\s*\{\s*isRecord\s*\}\s*from\s*["']\.\/comment-shared\.js["'];/,
    "const isRecord = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);",
  );
  return import(`data:text/javascript;base64,${Buffer.from(patched).toString("base64")}`);
}

function loadValidateCommandNameFromSource(srcRoot) {
  const regPath = path.join(srcRoot, "src/plugins/command-registration.ts");
  const rawTs = fs.readFileSync(regPath, "utf8");
  const stripped = moduleApi.stripTypeScriptTypes(rawTs, {
    mode: "strip",
    sourceUrl: pathToFileURL(regPath).href,
  });
  const patched = stripped
    .replace(
      /import\s*\{[\s\S]*?\}\s*from\s*["']@openclaw\/normalization-core\/string-coerce["'];/,
      `const normalizeLowercaseStringOrEmpty = (v) => (typeof v === "string" ? v.trim().toLowerCase() : "");
const normalizeOptionalLowercaseString = (v) => { const t = typeof v === "string" ? v.trim().toLowerCase() : ""; return t || undefined; };`,
    )
    .replace(/import[\s\S]*?from\s*["']\.\.\/gateway\/operator-scopes\.js["'];/, "const isOperatorScope = () => true;")
    .replace(/import[\s\S]*?from\s*["']\.\.\/globals\.js["'];/, "const logVerbose = () => {};")
    .replace(/import[\s\S]*?from\s*["']\.\.\/utils\.js["'];/, "const isRecord = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);")
    .replace(/import[\s\S]*?from\s*["']\.\/agent-prompt-surface-kind\.js["'];/, "const normalizeAgentPromptSurfaceKind = (v) => v;")
    .replace(
      /import[\s\S]*?from\s*["']\.\/command-registry-state\.js["'];/,
      "const clearPluginCommands = () => {}; const clearPluginCommandsForPlugin = () => {}; const isPluginCommandRegistryLocked = () => false; const pluginCommands = new Map();",
    )
    .replace(
      /import[\s\S]*?from\s*["']\.\/types\.js["'];/,
      "const AGENT_PROMPT_SURFACE_KINDS = [];",
    );
  return import(`data:text/javascript;base64,${Buffer.from(patched).toString("base64")}`);
}

describe("adapter packaging and plugin contract (always offline)", () => {
  it("matches OpenClaw 2026.6.5 plugin manifest and registration requirements", () => {
    const manifest = JSON.parse(
      fs.readFileSync(path.join(HERE, "openclaw.plugin.json"), "utf8"),
    );
    const pkg = JSON.parse(fs.readFileSync(path.join(HERE, "package.json"), "utf8"));
    const exampleFixture = JSON.parse(
      fs.readFileSync(path.join(HERE, "test-card.example.json"), "utf8"),
    );

    assert.equal(manifest.id, "feishu-recovery");
    assert.equal(pluginEntry.id, manifest.id);
    assert.equal(manifest.configSchema.additionalProperties, false);
    assert.deepEqual(pkg.openclaw.extensions, ["./index.mjs"]);
    assert.deepEqual(exampleFixture.card, buildSideEffectFreeTestCard());

    const registered = [];
    pluginEntry.register({
      pluginConfig: {},
      registerInteractiveHandler() {},
      registerCommand(cmd) {
        registered.push(cmd);
      },
    });
    assert.equal(registered.length, 1);
    assert.equal(registered[0].name, WECHAT_RECOVERY_COMMAND_NAME);
    assert.deepEqual(registered[0].channels, ["feishu"]);
    assert.equal(registered[0].acceptsArgs, true);
    assert.equal(registered[0].requireAuth, true);
  });
});

describe("OpenClaw 2026.6.5 official source probes (OPENCLAW_SRC_DIR)", () => {
  it("decodes ocf1 card payloads using real OpenClaw 2026.6.5 card-interaction.ts", async (t) => {
    if (!OPENCLAW_SRC_DIR) {
      t.skip("OPENCLAW_SRC_DIR not set");
      return;
    }
    const mod = await loadCardInteractionModuleFromSource(OPENCLAW_SRC_DIR);
    const exampleFixture = JSON.parse(
      fs.readFileSync(path.join(HERE, "test-card.example.json"), "utf8"),
    );
    const testValue = exampleFixture.card.body.elements[1].columns[0].elements[0].behaviors[0].value;
    const decodedTest = mod.decodeFeishuCardAction({
      event: {
        operator: { open_id: "ou_example_operator_01" },
        action: { tag: "button", value: testValue },
        context: { chat_id: "oc_example_recovery_chat" },
      },
    });
    assert.deepEqual(decodedTest, {
      kind: "structured",
      envelope: testValue,
    });

    const recoveryValue = {
      oc: "ocf1",
      k: "quick",
      a: "wechat-recovery:regen",
      q: "/wechat_recovery choose wechat-login:out-1:2 regen",
      c: { t: "group" },
    };
    const decodedRecovery = mod.decodeFeishuCardAction({
      event: {
        operator: { open_id: "ou_example_operator_01" },
        action: { tag: "button", value: recoveryValue },
        context: { chat_id: "oc_example_recovery_chat" },
      },
    });
    assert.equal(decodedRecovery.kind, "structured");
    assert.equal(decodedRecovery.envelope.k, "quick");
    assert.equal(decodedRecovery.envelope.q, "/wechat_recovery choose wechat-login:out-1:2 regen");
    assert.deepEqual(decodedRecovery.envelope.c, { t: "group" });

    // 验证当绑定 c.u / c.h 时 OpenClaw 拒绝不匹配的操作者或群
    const mismatch = mod.decodeFeishuCardAction({
      event: {
        operator: { open_id: "ou_example_operator_01" },
        action: {
          tag: "button",
          value: { ...recoveryValue, c: { u: "ou_other", t: "group" } },
        },
        context: { chat_id: "oc_example_recovery_chat" },
      },
    });
    assert.deepEqual(mismatch, { kind: "invalid", reason: "wrong_user" });

    const chatMismatch = mod.decodeFeishuCardAction({
      event: {
        operator: { open_id: "ou_example_operator_01" },
        action: {
          tag: "button",
          value: { ...recoveryValue, c: { h: "oc_other_group", t: "group" } },
        },
        context: { chat_id: "oc_example_recovery_chat" },
      },
    });
    assert.deepEqual(chatMismatch, { kind: "invalid", reason: "wrong_conversation" });
  });

  it("validates command definition via real OpenClaw 2026.6.5 command-registration.ts", async (t) => {
    if (!OPENCLAW_SRC_DIR) {
      t.skip("OPENCLAW_SRC_DIR not set");
      return;
    }
    const mod = await loadValidateCommandNameFromSource(OPENCLAW_SRC_DIR);
    assert.equal(mod.validateCommandName(WECHAT_RECOVERY_COMMAND_NAME), null);

    let capturedCommand = null;
    pluginEntry.register({
      pluginConfig: {},
      registerInteractiveHandler() {},
      registerCommand(cmd) {
        capturedCommand = cmd;
      },
    });
    assert.equal(mod.validatePluginCommandDefinition(capturedCommand), null);
    const res = mod.registerPluginCommand("feishu-recovery", capturedCommand);
    assert.deepEqual(res, { ok: true });
    mod.clearPluginCommands();
  });

  it("verifies pinned OpenClaw 2026.6.5 limitations and no-LLM path invariants in source", (t) => {
    if (!OPENCLAW_SRC_DIR) {
      t.skip("OPENCLAW_SRC_DIR not set");
      return;
    }
    const read = (rel) => fs.readFileSync(path.join(OPENCLAW_SRC_DIR, rel), "utf8");

    // 1. Feishu 扩展未使用 registerInteractiveHandler / dispatchPluginInteractiveHandler
    const feishuSrcDir = path.join(OPENCLAW_SRC_DIR, "extensions/feishu/src");
    const feishuFiles = fs.readdirSync(feishuSrcDir).filter((f) => f.endsWith(".ts"));
    const interactiveRefs = feishuFiles.filter((f) =>
      fs.readFileSync(path.join(feishuSrcDir, f), "utf8").includes("dispatchPluginInteractiveHandler"),
    );
    assert.deepEqual(interactiveRefs, []);

    // 2. monitor.account.ts 中 card.action.trigger 无任何同步 toast/换卡返回值（仅返回 void/undefined）
    const monitorAccount = read("extensions/feishu/src/monitor.account.ts");
    const triggerIdx = monitorAccount.indexOf('"card.action.trigger":');
    assert.ok(triggerIdx >= 0, "expected card.action.trigger handler");
    const triggerSnippet = monitorAccount.slice(triggerIdx, triggerIdx + 700);
    assert.match(triggerSnippet, /await promise;/);
    assert.doesNotMatch(triggerSnippet, /return\s+\{/);

    // 3. reply-dispatcher.ts 只取 text 与 media，不渲染 channelData.feishu 自定义卡片
    const replyDispatcher = read("extensions/feishu/src/reply-dispatcher.ts");
    assert.match(replyDispatcher, /resolveSendableOutboundReplyParts/);
    assert.doesNotMatch(replyDispatcher, /channelData\??\.feishu/);

    // 4. card-action.ts 若缺少 c.t 会尝试 im.chat.get 并在失败时回退为 p2p；bot.ts 按 chat/user 构造 To
    const cardAction = read("extensions/feishu/src/card-action.ts");
    assert.match(cardAction, /resolveCardActionChatType/);
    assert.match(cardAction, /return "p2p";/);
    const bot = read("extensions/feishu/src/bot.ts");
    assert.match(bot, /feishuFrom = `feishu:\$\{ctx\.senderOpenId\}`/);
    assert.match(
      bot,
      /feishuTo = isGroup \? `chat:\$\{ctx\.chatId\}` : `user:\$\{ctx\.senderOpenId\}`/,
    );

    // 5. plugins/commands.ts 对外部插件隐去 senderIsOwner，仅受信任暴露或保留命令例外
    const pluginCommands = read("src/plugins/commands.ts");
    assert.match(pluginCommands, /canExposeSenderIsOwner/);
    assert.match(pluginCommands, /isTrustedReservedCommandOwner/);
    const cmdRegistration = read("src/plugins/command-registration.ts");
    assert.match(cmdRegistration, /trustedOwnerStatusExposure/);

    // 6. 命令处理器优先于内置命令与大模型回复执行（handlePluginCommand 位于首位）
    const handlersRuntime = read("src/auto-reply/reply/commands-handlers.runtime.ts");
    assert.match(handlersRuntime, /return \[\s*handlePluginCommand,/);
    const commandsPlugin = read("src/auto-reply/reply/commands-plugin.ts");
    assert.match(commandsPlugin, /const shouldContinue = result\.continueAgent === true;/);
    assert.match(commandsPlugin, /shouldContinue,/);
  });
});

describe("OpenClaw 2026.6.5 installed package runtime loader probe (OPENCLAW_PKG_DIR)", () => {
  it("loads plugin via the real OpenClaw loader", async (t) => {
    if (!OPENCLAW_PKG_DIR) {
      t.skip("OPENCLAW_PKG_DIR not set");
      return;
    }
    const distDir = path.join(OPENCLAW_PKG_DIR, "dist");
    let loadOpenClawPlugins;
    for (const file of fs.readdirSync(distDir).filter((name) => /^loader-.*\.js$/.test(name))) {
      const mod = await import(pathToFileURL(path.join(distDir, file)).href);
      loadOpenClawPlugins = Object.values(mod).find(
        (value) => typeof value === "function" && value.name === "loadOpenClawPlugins",
      );
      if (loadOpenClawPlugins) break;
    }
    assert.ok(loadOpenClawPlugins, "loadOpenClawPlugins export not found");
    const registry = loadOpenClawPlugins({
      cache: false,
      onlyPluginIds: ["feishu-recovery"],
      config: {
        plugins: {
          allow: ["feishu-recovery"],
          load: { paths: [HERE] },
          entries: { "feishu-recovery": { enabled: true, config: {} } },
        },
      },
    });
    assert.ok(registry.plugins.some((p) => p.id === "feishu-recovery" && p.enabled));
  });
});
