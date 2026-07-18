# 微信客服 API 文本入口（最小闭环）

## 本阶段结论

本轮不新建独立 Linux 服务，直接复用 `aliecs` 上已有的
`backend-api` 和 HTTPS 入口：

```text
个人微信 -> 微信客服 -> hydwang.xyz
  -> backend-api 验签/解密 -> sync_msg
  -> echo 或可选 OpenAI-compatible 处理器
  -> send_msg -> 个人微信
```

四台设备分工：

- `devbox`：开发和测试，不运行生产。
- `aliecs`：唯一公网回调入口，负责收、拉、转、回。
- `webdock2`：以后启用 AI 处理时的当前主节点。
- `webdock1`：以后启用 AI 处理时的备用节点。

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
4. 只处理 `origin=3` 且 `msgtype=text` 的微信客户消息；客服人员消息
   `origin=5` 必须跳过，防止回显死循环。
5. 回复 ID 由源 `msgid` 确定性派生，重复回调不会生成新回复 ID。
6. 生产部署默认把文本转发到 ECS 本机 `openclaw-bridge`；会话使用
   `wecom / kf:<open_kfid> / <external_userid>` 隔离。处理器失败时降级为
   `已收到：<原文>`。

处理器使用 ECS 本机 `openclaw-bridge` 的 OpenAI-compatible 入口：

```env
WECOM_KF_PROCESSOR_URL=http://host.docker.internal:18080/v1/chat/completions
```

它会继续经现有 WebDock 主备路由到 `webdock2` / `webdock1`。如需只验收
echo，可显式把 `WECOM_KF_PROCESSOR_URL` 留空并直接启动 compose；正式
`deploy.sh` 默认启用上述本机入口。

## 企微B凭据映射

生产部署未单独配置 `WECOM_KF_*` 时，默认复用企微B现有的 CorpID、
自建应用 Secret、回调 Token 和 EncodingAESKey。`deploy.sh` 只在生成
运行时环境时做变量映射，不复制明文到 Git。若以后需要轮换或隔离，可在
SOPS 中显式设置专用的 `WECOM_KF_*` 覆盖默认值。

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

官方页面还明确：自建应用必须被配置到“微信客服 - 可调用接口的应用”；
2023-12-01 起新接入不再依赖系统应用 Secret。因此本实现用
`WECOM_KF_APP_SECRET` 表示被授权的自建应用 Secret。

## 尚未做（明确留给后续）

- 图片/语音/文件下载和回复。
- PostgreSQL/SQLite 消息库、游标持久化、运维查询界面。
- 消息发送失败事件的独立告警。
- 企业微信后台保存、生产发布和个人微信实发。

当前游标仅进程内保存；重启后可能重拉最近消息，但固定回复 ID
会降低重复回复风险。这是本最小阶段的已知边界，不应宣称为完整生产实现。
