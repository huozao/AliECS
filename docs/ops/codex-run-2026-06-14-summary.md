# Codex 3h 收尾执行汇总（2026-06-14）

## 分支

- AliECS：`codex/project-completion-2026-06-14`
- webdock：`codex/project-completion-2026-06-14`
- 未 push、未开 PR、未合并 main。

## Phase 完成度

| Phase | 状态 | 交付物 |
| --- | --- | --- |
| Phase 0 基线 | done | `docs/ops/codex-run-2026-06-14-baseline.md` |
| Phase 1 审计分页 | done | `/v1/admin/audit-logs` 分页，admin UI 默认折叠审计面板 |
| Phase 2 couple 图片 | done | 本地 `/app/uploads` 持久卷、`/uploads/{name}` 取回、webdock 失败回落本地 |
| Phase 3 T+ 价格 | 仅离线，待 ops 实拉 | `purchase_price` / `sales_price` transform/sync/export；`docs/ops/tplus-price-verify-2026-06-14.md` |
| Phase 4 控制面板 | done，待 ops 建表 | `managed_contacts`、doc-sync worksheet upsert、routing API、webdock 拉取器；`docs/ops/control-plane-sheet-schema.md` |
| Phase 5 飞书一对一 | 仅离线，待人工 DM 验证 | bridge/webdock 飞书 lane 隔离；`docs/ops/feishu-channel-verify-2026-06-14.md` |
| Phase 6 加微信入口 | 仅离线，待 ops 配二维码来源 | `/v1/ops/wechat/login-qr`、`/health/` 添加新微信弹窗；`docs/ops/wechat-add-verify-2026-06-14.md` |
| Phase 7 企微B历史 | done，历史结论为不可直接拉取 | `wecom_b_messages` 前向捕获；`docs/ops/wecom-b-group-history-spike-2026-06-14.md` |

## 新增/扩展测试

- AliECS：22 个用例。
- webdock：4 个用例。
- 合计：26 个用例。

## 外部实拉/实时验证

- T+ 采购/销售价格：离线 fixtures 全绿；实时 job 均重试 3 次后 HTTP 403，`message=openToken已失效`，未产出 xlsx。
- 飞书：ECS `openclaw-openclaw-gateway-1` healthy，`FEISHU_APP_ID/FEISHU_APP_SECRET` 已注入；最近 5 分钟日志无 feishu/lark/websocket 匹配输出，需人工发 DM 验证。
- 加微信二维码：ECS backend 未配置 `OPENCLAW_WECHAT_LOGIN_QR_URL/FILE`；OpenClaw 文档仅确认 CLI 登录方式 `openclaw channels login --channel openclaw-weixin`。
- 企微B历史：智能机器人/长连接只适合接入后新消息推送；历史/全量需会话内容存档能力和 SDK。

## 待人工/ops 清单

- 刷新 T+ `CHANJET_OPEN_TOKEN` 后复跑两个价格 job，并抽样对比用户导出 Excel。
- 在企微A智能表「管理面板」创建/规范 `微信用户清单`、`飞书用户清单`。
- 配置 webdock 定时调用 routing puller 或在部署脚本中周期执行，确保 `wechat_projects.json` / `feishu_projects.json` 更新。
- 飞书开放平台确认事件订阅、机器人能力、联系人/消息权限，发 DM 做端到端验证。
- OpenClaw Weixin 新账号登录仍需人工扫码；如要 `/health/` 弹窗显示二维码，需配置二维码 URL/文件来源。
- 企微B如需历史消息，走企业微信会话内容存档合规路径；当前代码只留存接入后的新消息。

## 最终测试

- AliECS：`PYTHONPATH=. pytest tests/ -q` → `235 passed, 2 skipped, 2 warnings`。
- webdock：`pytest -q` → `111 passed`。

## 提交

AliECS：

- `764280a chore(codex): 记录3h收尾任务基线`
- `ad0ead8 feat(admin): 审计日志分页+默认折叠`
- `30fa6ca fix(couple): 本地图片持久卷+取回路由`
- `a712434 feat(tplus): 实现采购销售价格同步导出`
- `6ff84c5 feat(control-plane): 企微表同步运行时联系人路由`
- `b4b1680 feat(feishu): bridge保留飞书lane元数据`
- `a5b7183 feat(ops): health添加微信二维码入口`
- `c666d31 docs(wecom-b): 群历史结论和前向捕获`
- `bc8e219 test(control-plane): 隔离doc-sync导入上下文`

webdock：

- `f11d7c9 feat(routing): 从backend拉取路由配置`
- `fe7413f feat(feishu): webdock支持飞书lane路由`
