# 合并 + 部署顺序清单（2026-06-14）

> 两条独立工作流：**Track 1 = 7项收尾**、**Track 2 = AdventureLog**。基线 `origin/main = eb75b95`。
> 冲突核查结论：两个 AliECS 分支**修改的文件零重叠**（couple/map 前端只在 AdventureLog 分支；main.py/admin/health/tplus/doc-sync/compose 只在 7项分支；docs/ops 与 tests 各加各的不同文件）→ **可干净合并，顺序不影响冲突**。唯一硬约束是 **AdventureLog 必须先把应用部署起来，再发它的前端**（否则死链）。

## 分支清单
| 仓 | 分支 | 内容 |
|---|---|---|
| AliECS | `codex/project-completion-2026-06-14` | 7项收尾(10 commits) |
| webdock | `codex/project-completion-2026-06-14` | 路由拉取+飞书lane(+我修的拉取器接线 `6ea503f`) |
| AliECS | `codex/adventurelog-couple-2026-06-14` | couple前端relink/map退役 + 迁移脚本 |
| infra | `codex/adventurelog-couple-2026-06-14` | AdventureLog 部署物料(compose/env/nginx/tunnel/runbook) |

---

## Track 1 — 7项收尾（先做，安全、即时见效）

**这批代码全部"优雅降级"——外部没配也不会崩，只是对应功能未激活。可放心先合先部署。**

- [ ] 1. **review** AliECS `codex/project-completion-2026-06-14`（10 commits；全量已 235 passed/2 skipped）。
- [ ] 2. **合并 main + 触发部署**（按你现有流程：合 main → push 触发 release 重建镜像 + SSH 部署）。
- [ ] 3. **DB 迁移**随部署执行 `0014_managed_contacts.sql`、`0015_wecom_b_messages.sql`（走 `deploy/ecs/migrate.sh`）。
- [ ] 4. **compose 生效**：`deploy/ecs/compose.prod.yml` 新增了 backend `uploads` 命名卷（couple 图片持久化），确保 `docker compose up -d` 应用。
- [ ] 5. **bridge 镜像**：7项改了 `deploy/openclaw-bridge/openclaw_bridge.py`（飞书 lane 元数据）→ release 会重建 `openclaw-bridge` 镜像；到 ECS 把 infra 的 `OPENCLAW_BRIDGE_TAG` 指到新 V 标签并 `up -d`。
- [ ] 6. **webdock 部署**：合并 webdock `codex/project-completion-2026-06-14` → push → 旧电脑拉新镜像。**给 webdock 容器配 `ALI_ECS_BACKEND_URL`（指向 backend 路由 API 可达地址）**，否则路由拉取器优雅 no-op（控制面板不下发）。

**部署后即时可用**：④ 审计分页/折叠、⑥ couple 图片(/uploads 持久 + 取回)。

**部署后要做的 ops（激活其余项）**：
- [ ] ② 控制面板：按 `docs/ops/control-plane-sheet-schema.md` 在企微A智能表建 `微信用户清单`/`飞书用户清单` 及列 → doc-sync 全量同步即 upsert `managed_contacts` → 路由 API 输出 → webdock 拉取器下发。
- [ ] ⑤ T+价格：**prod token 是新鲜的**（实测今天更新），在 tplus 容器内跑 `job_sync_purchase_price` / `job_sync_sales_price`，对比用户导出 Excel 抽样（详见 `docs/ops/tplus-price-verify-2026-06-14.md`）。
- [ ] ① 飞书：飞书开放平台后台配事件订阅/长连接/权限并发 DM 端到端验证（`docs/ops/feishu-channel-verify-2026-06-14.md`）。当前网关日志无 websocket 连接，说明后台还没配通。
- [ ] ③ 加微信二维码：端点脚手架在，但 OpenClaw 仅 CLI 终端二维码登录、无导出 → 留作后续小任务（见本仓后续设计）。
- [ ] ⑦ 企微B：前向捕获已上线；历史消息拿不到（需会话内容存档合规路径）。

---

## Track 2 — AdventureLog（后做，部署顺序是硬约束）

**死链坑**：AliECS AdventureLog 分支把 couple 的"地图/相册"入口指向 `https://adventure.hydwang.xyz`、`/map/` 跳转过去。**该域名没上线前，绝不能合并/部署这个 AliECS 前端分支**，否则用户点到死链/证书错。

正确顺序：
- [ ] 1. **review + 合并 infra** `codex/adventurelog-couple-2026-06-14`（纯部署物料文件，安全）。
- [ ] 2. **先把 AdventureLog 应用部署上线**（按 `infra/docs/runbook-adventurelog.md` / `AliECS docs/ops/adventurelog-deploy-2026-06-14.md` 的 Phase D）：旧电脑核内存 → 填 `.env` 密钥 → compose up → 起隧道 unit → ECS nginx 两子域 + certbot TLS → 关注册 → 建 2 用户 → 各账号配 Immich API key → 验收 `adventure.hydwang.xyz` 可登。
- [ ] 3. **核对迁移脚本对接**：按 AdventureLog v0.12.1 真实 API 核对 adventure 字段/创建端点/Immich asset 关联端点（脚本里已标 TODO），先 `python -m scripts.adventurelog.migrate_memories`（dry-run）看对账，确认再 `--apply`。
- [ ] 4. **域名上线且验收通过后**，再 **review + 合并 + 部署 AliECS** `codex/adventurelog-couple-2026-06-14`（couple relink + /map/ 退役）。

---

## 跨切提醒
- 两个 AliECS 分支无冲突，但**分别基于 `eb75b95`**；先合的那个进 main 后，另一个合并前可 `git rebase origin/main` 一下（大概率无冲突）保持线性。
- 我写的规划/spec/runbook 文档（含本文件）目前多为 **untracked**，合并分支不会带上；如要入库我可单独提交。
- webdock 路由 API 是**公开端点**（无鉴权），上线后建议后续加共享密钥头（次要，先记着）。
- 部署后别忘核对：`release-deploy` 重建镜像会**覆盖任何未回灌 git 的热补丁**（热补丁必须先回 git，见既有红线）。
