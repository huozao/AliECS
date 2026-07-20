# 微信客服 API：对话与资料任务

## 本阶段结论

本轮不新建独立 Linux 服务，直接复用 `aliecs` 上已有的
`backend-api` 和 HTTPS 入口：

```text
个人微信 -> 微信客服 -> hydwang.xyz
  -> backend-api 验签/解密 -> sync_msg
  -> 普通文本：OpenAI-compatible 处理器 -> 文本回复
  -> 资料任务：保存原件 -> AI 分析 -> 微信菜单确认 -> 生成归档清单
```

四台设备分工：

- `devbox`：开发和测试，不运行生产。
- `aliecs`：唯一公网回调入口，负责收、拉、保存、编排和回复。
- `webdock2`：ChatGPT 浏览器自动化主节点，负责普通对话和资料分析。
- `webdock1`：ChatGPT 浏览器自动化备用节点。

回调地址：

```text
https://hydwang.xyz/api/v1/webhooks/wecom-kf
```

Nginx 现有 `/api/ -> 127.0.0.1:8000/` 已覆盖该路径，本阶段无需改
`infra` 主机配置。

## 处理规则

1. GET 回调验证：校验 `msg_signature`，解密 `echostr`，原样返回明文。
2. POST 事件：先校验签名并解密 XML，只接受 `kf_msg_or_event`。
3. 快速返回 `success`，后台用事件内的临时 `Token` 调 `sync_msg`。
4. 只处理 `origin=3` 的微信客户文本、图片和文件；客服人员消息
   `origin=5` 必须跳过，防止回显死循环。语音、视频和其他类型暂不处理。
5. 回复 ID 由源 `msgid` 确定性派生，重复回调不会生成新回复 ID。
6. 生产部署默认把文本和受支持附件转发到 ECS 本机 `openclaw-bridge`；会话使用
   `wecom / kf:<open_kfid> / <external_userid>` 隔离。处理器失败时降级为
   `已收到：<原文>`。

处理器使用 ECS 本机 `openclaw-bridge` 的 OpenAI-compatible 入口：

```env
WECOM_KF_PROCESSOR_URL=http://host.docker.internal:18080/v1/chat/completions
```

它会继续经现有 WebDock 主备路由到 `webdock2` / `webdock1`。如需只验收
echo，可显式把 `WECOM_KF_PROCESSOR_URL` 留空并直接启动 compose；正式
`deploy.sh` 默认启用上述本机入口。

## 资料任务用法

推荐流程：

1. 发送 `开始任务：任务名称`（也可直接先发图片或文件自动建任务）。
2. 连续发送说明、图片和文件；中间不逐条回复，避免消耗客服回复额度。
3. 发送 `发送完毕`。服务把说明和可识别附件交给 ChatGPT 分析，但不会直接执行任意系统命令。
4. 微信返回分析结果和固定菜单：`确认处理`、`补充资料`、`取消任务`。
5. 只有选择 `确认处理` 后，任务才标记完成，并在持久卷中生成 `manifest.json` 和 `analysis.md`。

`补充资料` 会恢复收集状态；分析失败时原件仍保留，可发送 `重试处理`。
任务、附件元数据、发送状态和 `sync_msg` 游标保存在 PostgreSQL；原件和归档结果保存在
`wecom_kf_materials` Docker 持久卷。单个下载附件上限 20MB；默认最多把 12 个、合计
40MB 的受支持附件交给处理器，超出部分仍保存但不交给模型。

## 企微B凭据映射

`WECOM_KF_CORP_ID` 可默认复用企微B的 CorpID；`WECOM_KF_APP_SECRET`
必须使用“微信客服 - 开发配置 - 企业内部接入”显示的专用 Secret，不能
回退到普通自建应用 Secret。回调 Token 和 EncodingAESKey 建议使用专用值，
并通过 SOPS 保存。`deploy.sh` 只在生成运行时环境时做变量映射，不复制明文
到 Git。

## 官方文档核实（2026-07-18）

- [回调配置](https://developer.work.weixin.qq.com/document/path/90930)，页面最后更新
  2024-12-13：GET 验证需 1 秒内返回无引号、无 BOM、无换行的解密明文；
  POST 建议立即应答、业务异步处理。
- [接收消息和事件](https://developer.work.weixin.qq.com/document/path/94670)，页面最后更新
  2024-12-23：回调只是通知，须用 `POST /cgi-bin/kf/sync_msg` 拉取；事件
  `Token` 10 分钟内有效；停止分页必须看 `has_more`，不能看列表是否为空。
- [发送消息](https://developer.work.weixin.qq.com/document/path/94677)，页面最后更新
  2026-02-27：文本用 `POST /cgi-bin/kf/send_msg`；当前官方页面说明为客户
  主动发送后 48 小时内最多下发 5 条。接口返回成功不代表最终送达。

企微B当前启用“企业内部接入”后，普通自建应用 Secret 调用客服接口实测返回
`95012 not use in wecom`；专用 Secret 可正常调用 `kf/account/list`。因此
`WECOM_KF_APP_SECRET` 专指该页面显示的客服专用 Secret。

## 当前边界

- 不支持语音、视频作为资料输入，也不从客服端主动发送附件。
- “确认处理”当前生成结构化归档清单和分析文件，不会按模型自由文本去移动其他业务目录或执行 Shell。
- 已记录 `msg_send_fail` 发送失败状态，但还没有独立告警和运维查询界面。
- 当前没有自动清理/外部备份策略，需结合磁盘容量另行制定保留策略。
- 个人微信实发验收。
