# 企微A 管理面板 worksheet 规范

## 需要人工/ops 创建或确认

在企微A智能表「管理面板」中创建或规范以下 worksheet：

- `微信用户清单`：同步为 `managed_contacts.channel = 'wechat'`
- `飞书用户清单`：同步为 `managed_contacts.channel = 'feishu'`

doc-sync 会按 worksheet 名识别渠道；表名不匹配时不会写入 `managed_contacts`。

## 推荐列

| 列名 | 必填 | 说明 |
| --- | --- | --- |
| `peer_id` | 是 | 渠道内唯一 ID；微信填 peer/wxid，飞书填 open_id/union_id 中当前 OpenClaw 实际上报的 peer 值。 |
| `display_name` | 否 | 显示名。 |
| `remark` | 否 | 备注/真名。 |
| `enabled` | 否 | 权限总开关；支持 `是/否/true/false/1/0/on/off`，空值按启用处理。 |
| `project_url` | 否 | ChatGPT 项目地址；启用且非空时进入运行时路由 JSON。 |
| `project_name` | 否 | 项目名称。 |
| `tags` | 否 | 逗号分隔标签/分组。 |
| `daily_quota` | 否 | 预留配额字段，整数。 |
| `notes` | 否 | 备注说明。 |

## 支持的别名

doc-sync 也兼容部分中文/旧列名：

- `peer_id`：`用户ID`、`渠道用户ID`、`微信ID`、`微信peer`、`飞书open_id`、`open_id`
- `display_name`：`昵称`、`显示名`、`用户名`、`姓名`
- `enabled`：`启用`、`是否启用`、`权限开关`、`权限`
- `project_url`：`ChatGPT项目地址`、`项目地址`、`project`
- `project_name`：`项目名称`、`ChatGPT项目名`
- `tags`：`标签`、`分组`
- `daily_quota`：`每日配额`、`配额`
- `notes`：`说明`、`备注说明`

## 运行链路

1. 人工编辑企微A智能表。
2. doc-sync 同步 worksheet 记录到 `managed_contacts`。
3. backend 输出：
   - `/v1/routing/wechat-projects.json`
   - `/v1/routing/feishu-projects.json`
4. webdock 拉取 `/v1/routing/wechat-projects.json` 覆盖本地 `wechat_projects.json`；backend 不可达时保留旧文件。

## 待人工验证

- `SMARTSHEET_COMPANY_A_*` 是否指向包含 `微信用户清单` / `飞书用户清单` 的管理面板文档。
- OpenClaw/webdock 实际上报的微信 `peer_id` 和飞书 `peer_id` 字段名及值是否与表内填写一致。
- 飞书用户清单的 `peer_id` 需要在 Phase 5 飞书端到端验证后复核。
