import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import { buildSideEffectFreeTestCard } from "./core.mjs";

const packagePrefix = process.argv[2];
assert.ok(packagePrefix, "传入隔离 npm 安装目录");
const modules = resolve(packagePrefix, "node_modules");
const larkPath = join(modules, "@larksuite/openclaw-lark");
const openClawPath = join(modules, "openclaw");
const recoveryPath = fileURLToPath(new URL(".", import.meta.url));
const appId = "cli_example";
const chatId = "oc_example";
const operatorId = "ou_example";
const decisionKey = "wechat-login:example:1";
const actionToken = "probe-local-only";
const choicePath = "/v1/internal/notify/decisions/feishu-choice";
const choiceBodies = [];
let botPings = 0;

for (const [root, expected] of [[openClawPath, "2026.6.5"], [larkPath, "2026.7.16"]]) {
  const pkg = JSON.parse(await readFile(join(root, "package.json"), "utf8"));
  assert.equal(pkg.version, expected, pkg.name);
}

const server = createServer(async (request, response) => {
  response.setHeader("Content-Type", "application/json");
  if (request.url === "/open-apis/auth/v3/tenant_access_token/internal") {
    return response.end(JSON.stringify({ code: 0, tenant_access_token: "probe-local", expire: 3600 }));
  }
  if (request.url === "/open-apis/bot/v1/openclaw_bot/ping") {
    botPings++;
    return response.end(JSON.stringify({ code: 0, data: { pingBotInfo: { botID: "ou_example_bot" } } }));
  }
  if (request.url !== choicePath || request.headers["x-notify-feishu-action-token"] !== actionToken) {
    response.statusCode = 400;
    return response.end("{}");
  }
  const parts = [];
  for await (const part of request) parts.push(part);
  const body = JSON.parse(Buffer.concat(parts));
  choiceBodies.push(body);
  response.end(JSON.stringify({ ok: true, decision_key: body.decision_key, option_key: body.option_key }));
});
const abort = new AbortController();
let accountRun;
const stateDir = await mkdtemp(join(tmpdir(), "feishu-lark-probe-"));
const configPath = join(stateDir, "openclaw.json");
try {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const base = `http://127.0.0.1:${server.address().port}`;
  const config = {
    channels: { feishu: { enabled: true, appId, appSecret: "probe-local", domain: base, connectionMode: "websocket" } },
    plugins: {
      allow: ["openclaw-lark", "feishu-recovery"],
      load: { paths: [larkPath, recoveryPath] },
      entries: {
        "openclaw-lark": { enabled: true },
        "feishu-recovery": {
          enabled: true,
          config: { aliecsBaseUrl: base, actionToken, allowedChatIds: [chatId], allowedOperators: [operatorId] },
        },
      },
    },
  };
  process.env.OPENCLAW_STATE_DIR = stateDir;
  process.env.OPENCLAW_CONFIG_PATH = configPath;
  process.env.no_proxy = "127.0.0.1,localhost";
  process.env.NO_PROXY = process.env.no_proxy;
  await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });

  const fromLark = createRequire(join(larkPath, "index.js"));
  const { WSClient } = fromLark("@larksuiteoapi/node-sdk");
  let client;
  let response;
  // Replace only the socket boundaries. The plugin registers the real dispatcher and
  // handles and encodes each card frame using the published Feishu SDK implementation.
  WSClient.prototype.start = function ({ eventDispatcher }) {
    this.eventDispatcher = eventDispatcher;
    client = this;
  };
  WSClient.prototype.sendMessage = function (frame) {
    response = JSON.parse(new TextDecoder().decode(frame.payload));
  };
  const dist = join(openClawPath, "dist");
  const candidates = (await readdir(dist)).filter((file) => /^loader-.*\.js$/.test(file));
  let load;
  for (const file of candidates) {
    const mod = await import(pathToFileURL(join(dist, file)).href);
    load = Object.values(mod).find((value) => typeof value === "function" && value.name === "loadOpenClawPlugins");
    if (load) break;
  }
  assert.ok(load, "OpenClaw 插件加载器");
  const registry = load({ config, cache: false, onlyPluginIds: config.plugins.allow });
  for (const id of config.plugins.allow) {
    const record = registry.plugins.find((plugin) => plugin.id === id);
    assert.equal(record?.status, "loaded", record?.error ?? id);
  }
  const channels = registry.channels.filter(({ plugin }) => plugin.id === "feishu");
  assert.equal(channels.length, 1);
  accountRun = channels[0].plugin.gateway.startAccount({
    cfg: config, accountId: "default", abortSignal: abort.signal,
    setStatus() {}, runtime: { log() {}, error: console.error },
  });
  for (let attempt = 0; !client && attempt < 100; attempt++) await delay(30);
  assert.ok(client, "飞书插件已注册 WebSocket 回调");
  assert.equal(botPings, 1, "机器人身份探测留在回环接口");

  async function click(id, value, operator = { open_id: operatorId }) {
    const headers = Object.entries({ type: "card", message_id: id, sum: "1", seq: "0", trace_id: id })
      .map(([key, value]) => ({ key, value }));
    const payload = Buffer.from(JSON.stringify({
      schema: "2.0",
      header: { event_type: "card.action.trigger", event_id: id, app_id: appId },
      event: { operator, action: { value }, context: { open_chat_id: chatId, open_message_id: "om_example" } },
    }));
    response = undefined;
    const start = performance.now();
    await client.handleEventData({ headers, payload });
    assert.equal(response?.code, 200);
    return {
      toast: response.data ? JSON.parse(Buffer.from(response.data, "base64")).toast : undefined,
      elapsedMs: Math.round(performance.now() - start),
    };
  }

  const testButton = buildSideEffectFreeTestCard().body.elements[1].columns[0].elements[0];
  const probe = await click("probe", testButton.behaviors[0].value);
  assert.equal(probe.toast?.type, "success");
  assert.ok(probe.elapsedMs < 3000, "无副作用卡在飞书回调时限内答复");
  assert.equal(choiceBodies.length, 0);
  const value = { action: `wechat-recovery:choose ${decisionKey} regen` };
  const business = await click("business", value);
  assert.equal(business.toast?.content, "已受理，正在生成二维码。");
  assert.ok(business.elapsedMs < 3000, "业务回调在飞书时限内答复");
  await click("duplicate", value);
  const denied = await click("denied", { action: `wechat-recovery:choose ${decisionKey} skip` }, { user_id: operatorId });
  assert.equal(denied.toast?.type, "error");
  assert.deepEqual(choiceBodies, [{
    decision_key: decisionKey, option_key: "regen", operator_open_id: operatorId, chat_id: chatId,
  }]);
  console.log(JSON.stringify({ probe, business, denied, submissions: choiceBodies.length }));
} finally {
  abort.abort();
  await accountRun;
  await new Promise((done) => server.close(done));
  await rm(stateDir, { recursive: true, force: true });
}
// The Feishu SDK keeps a frame-cache interval after its WS client closes.
process.exit(0);
