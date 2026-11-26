# 微信和 Telegram 登录恢复卡片

`feishu-recovery` 将「重新生成二维码」和「暂不处理」交给 AliECS 消息中枢。中枢确认写入后，飞书立即显示受理提示，并用不可点击的「正在生成新二维码」替换旧卡上的按钮。WebDock2 从决策 SSE 获得唤醒信号。新二维码通过独立通知发回飞书，且仅在同群、同掉线的下一尝试取得带图发送回执时，旧卡标记「新二维码已发出」。


## 固定版本与接手

- 中枢与网关适配器以 [AliECS 合并提交](https://github.com/huozao/AliECS/commit/acf00b476c87a406d4fa4d859afe5e60a7fe2e07) 为准。业务中枢的正式发布查看 [GitHub 发布执行](https://github.com/huozao/AliECS/actions/runs/45828246969) 并核对设备的发布 SHA、原生按钮开关和网关插件状态。
- WebDock2 的 [wechat-digest 固定提交](https://github.com/nihil7/wechat-digest/commit/7970a17cfe16c4d6d85ad7c554b5a3a34e896246) 已发布。微信 watcher、Telegram 定时器和待人工观察项见 [业务进度](https://github.com/nihil7/wechat-digest/issues/1)。
- 真人点击首次验证原生弹窗，重复点击由官方插件去重。正式业务关注同一次决策是否只受理一次、旧卡是否立即移除按钮，以及收到带图回执才标记「新二维码已发出」。生产受限配置保留在环境中。

## 两种飞书回调

现网 OpenClaw `2026.6.5` 的内置飞书通道识别卡片的 `ocf1` 数据，通过固定命令 `/wechat_recovery` 异步回复文字。该通道的回调返回值为空。

`@larksuite/openclaw-lark@2026.7.16` 识别卡片的 `action: "wechat-recovery:choose ..."`，通过 `api.registerInteractiveHandler` 直达同一个业务处理器，并将 `{toast: {type, content}}` 回传飞书。AliECS 新卡同时携带两种通道的路由信息，网页确认入口也保留。两个固定入口均绕过模型及对话桥接。参见[飞书回调时限](https://open.feishu.cn/document/event-subscription-guide/callback-subscription/receive-and-handle-callbacks)。

- 官方插件回调仅接受飞书事件的 `operator.open_id`，并校验其与插件提供的发送者一致。群取自飞书事件上下文。群与操作者都须命中允许名单。中枢用专用令牌再次校验两名单，只接受当前微信恢复决策。
- 原生弹窗路径给中枢请求预留最多 1.8 秒。匹配的确认响应显示受理。超时或结果不明确时提示核对状态，无自动重放。
- 网页确认默认启用。启用原生按钮后，卡片仍附网页确认入口。相同选择由中枢去重，WebDock2 的二维码执行还保留尝试号及执行状态检查。

## 本地验收

从本目录执行。探针安装两个固定版本并使用回环服务、模拟卡片帧和真实 SDK；飞书网络端点、生产密钥和业务数据均无需提供。

```sh
npm test
probe_dir=$(mktemp -d)
npm install --prefix "$probe_dir" --ignore-scripts --no-audit --no-fund --save-exact \
  openclaw@2026.6.5 @larksuite/openclaw-lark@2026.7.16
node ./probe-lark-runtime.mjs "$probe_dir"
```

探针由 OpenClaw 插件加载器读取官方发布包的 `openclaw.extensions` 入口。该 npm 包所声明的通用 ESM 入口缺失，直接 `import("@larksuite/openclaw-lark")` 会失败；OpenClaw 加载器与飞书 SDK 的卡片处理实测通过。探针检查无副作用卡、受理弹窗、重复点击单次提交、缺少 `open_id` 的拒绝。数据库写入与 SSE 另见[消息中枢回归](../../docs/runbooks/notify.md#实时唤醒-sse-与微信恢复原生入口)。

2026-11-26 用与现网相同的 Node.js `24.16.0` 跑过隔离验收。模拟卡片帧经真实插件和 SDK 到实际中枢及 PostgreSQL，只有一条决策历史，SSE 收到一次提交后的唤醒。飞书客户端显示、现网普通对话和实际二维码送达待线上验收。

同日网关切到官方飞书插件，OpenClaw 镜像保留 `2026.6.5`。测试模式 `probeOnly: true` 仅允许指定群的无副作用卡片回执，并拒绝所有业务选择。现有飞书长连接已就绪，测试卡已发送并通过飞书 API 回读。用户在飞书客户端首次点击看到「回调测试成功，未发起二维码任务」，再次点击同一按钮未显示第二次弹窗，符合该版本回调去重的隔离测试结果。该试切阶段关闭了业务原生按钮，新增飞书工具在测试期禁用。普通会话收发与真实二维码送达待验收。

```sh
node ./render-test-card.mjs default
```

上面的命令只输出卡片 JSON。授权群和操作者点击后应看到「回调测试成功，未发起二维码任务」。`probeOnly: true` 时，回调须有飞书操作者 `open_id` 并来自指定群，业务选择统一拒绝。该卡不提交决策或生成二维码。

## 插件切换与回退

1. 备份当前声明的镜像、插件允许名单和 `channels.feishu` 配置。保持 OpenClaw 镜像版本，同一个飞书 App ID 在切换时仅运行一个长连接。预检查发布配置，将通道插件 ID 由内置的 `feishu` 切为 `openclaw-lark`，加入 `feishu-recovery`。飞书通道键仍是 `channels.feishu`，保留该账户的凭据、`domain` 与消息门控。保留其他渠道和现有插件名单。对当前配置中只有内置插件识别的字段先做行为验证，例如 `typingIndicator`。
2. 将两个插件固定在发布目录并按网关的配置管理方式安装，保留验证时使用的依赖锁文件。使用 GitHub 固定 SHA 的本目录源码，第三方依赖以 [vendor 锁文件](vendor/package-lock.json) 执行 `npm ci --legacy-peer-deps --ignore-scripts`，通过在依赖目录将 `openclaw` 指向随镜像提供的运行时。生产凭据只存放在宿主运行配置。在插件的 `config` 设置 `aliecsBaseUrl`、`allowedChatIds`、`allowedOperators`，或由网关注入对应环境变量。独立动作令牌通过 `NOTIFY_FEISHU_RECOVERY_GATEWAY_TOKEN` 注入网关和中枢。完整字段见 [manifest](openclaw.plugin.json) 和[中枢配置](../../docs/runbooks/notify.md#生产需设项)。
3. 先保持 `NOTIFY_WECHAT_RECOVERY_NATIVE_CARDS` 关闭。检查只有一个飞书通道加载，网关长连就绪，现有群聊、私聊和桥接收发通过。用无副作用测试卡确认飞书端实际显示弹窗、未调用中枢。再启用原生按钮，验证一次真实受理、一次 WebDock2 执行和二维码消息送达。
4. 出现问题时先关闭原生按钮开关，新卡回到网页确认入口，已有卡也可使用其中的网页链接。如需回退通道，停用官方插件并恢复先前的插件与通道配置。中枢 SSE 可独立回退或保留。已收到的真实原生卡可能仍可回调，一并关闭网关恢复插件的业务提交或撤销专用令牌。数据库迁移保留新增收据列，回退期间无需改写通知历史。

卡片状态复用中枢既有的可靠补投循环。在已受理四秒后允许 PATCH，以免覆盖飞书同步替换的卡片。带图回执未确认时只显示「正在生成」，两分钟后显示「送达尚未确认」。刷新失败保留状态重试，任何一次卡片更新都不会重新运行扫码流程。
