import {
  WECHAT_RECOVERY_COMMAND_NAME,
  WECHAT_RECOVERY_INTERACTIVE_NAMESPACE,
  handleRecoveryCommand,
  handleRecoveryInteractive,
} from "./core.mjs";

const plugin = {
  id: "feishu-recovery",
  name: "Feishu WeChat Recovery Card Bridge",
  description:
    "Routes wechat-digest Feishu card actions to AliECS with a native callback toast or command reply.",
  register(api) {
    api.registerInteractiveHandler({
      channel: "feishu",
      namespace: WECHAT_RECOVERY_INTERACTIVE_NAMESPACE,
      handler: (ctx) => handleRecoveryInteractive(ctx, { pluginConfig: api.pluginConfig }),
    });
    api.registerCommand({
      name: WECHAT_RECOVERY_COMMAND_NAME,
      description:
        "Handle AliECS wechat-digest recovery card actions (fixed command only, never delegates to LLM).",
      channels: ["feishu"],
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx) =>
        handleRecoveryCommand(ctx, {
          pluginConfig: api.pluginConfig,
        }),
    });
  },
};

export default plugin;
