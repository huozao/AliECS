# 全设备版本看板（version-inventory）设计

日期：2026-07-17 · 状态：已与用户逐项确认（推送=B 周报、对比=B 全镜像、设备=A 仅 Linux 面、通道=A backend 直发飞书）

## 背景与目标

2026-07-17 安全评估暴露的真实痛点：OpenClaw 按架构决议禁自动升级，安全公告（如 CVE-2026-25253）只能人工跟，但没有任何机制提醒"该看了"；Immich 等第三方自托管服务属"装完就忘"型，CVE 活跃却无人对版本。目标：

1. /health 看板新增「版本」区块：设备 × 组件 × 当前版本 × 上游最新 × 状态。
2. 每周一 09:00（北京）飞书周报：落后组件、apt security 更新数、未登记镜像、缺席设备；**全绿也发短讯**（沉默必须可判定为管道故障）。
3. 人工扫一眼 → 把要更新的项丢给 AI 执行，形成闭环。

## 非目标

- 不做自动更新（只提醒，更新永远人工触发）。
- 不采集 Windows 面（webdock2 宿主 / devbox 的 Windows Update 留二期）。
- 自家镜像（AliECS 业务、webdock、bridge）不查上游——release 流程已保证最新，只做**跨设备 tag 一致性核对**（fleet.md"两机同 tag"纪律机器化）。
- 不比较 OS 发行版大版本（apt 只报可升级数量）。

## 架构

```
aliecs / webdock1 / webdock2(WSL)  systemd timer 每日 05:00±错峰
  └─ infra/versions/collect-versions.sh
       ├─ docker ps 全容器: 镜像名+tag+RepoDigest（全量上报，不按清单）
       ├─ aliecs 额外: docker exec openclaw gateway --version（镜像 sha256 锁定，tag 无版本号）
       ├─ apt-get -s upgrade 计数: 总数 + security 数
       ├─ POST https://hydwang.xyz/api/v1/internal/versions/report（X-Backup-Report-Token，复用现有 token）
       └─ 成功后 POST /v1/internal/backups/report 报心跳 run（复用 stale 告警，零新代码）

aliecs backend（backend-api）
  ├─ 存 version_reports；按镜像名匹配 version_components；未匹配 → "未登记"
  ├─ timer 触发 /v1/internal/versions/refresh-upstream：查 GitHub releases / Docker Hub API
  ├─ GET /v1/ops/versions：看板数据（require_admin）
  └─ timer 触发 /v1/internal/versions/weekly-digest：汇总 → 飞书 im API 直发
```

## 数据模型（迁移 `0037_version_inventory.sql`）

三张新表 + 三行心跳 policy：

```sql
version_components (          -- 配置面，看板可见
  component_key text PK,      -- 如 'immich-server'
  display_name text,
  kind text,                  -- docker-image | apt-summary | binary
  match_images text[],        -- 匹配上报镜像名，如 {'ghcr.io/immich-app/immich-server'}
  devices text[],             -- 限定设备（NULL=任意）；postgres 在 aliecs/webdock1 各自成组件，靠此区分
  upstream_source text,       -- github-release | dockerhub | none(自家/锁定)
  upstream_ref text,          -- 'immich-app/immich' 或 'library/postgres'
  version_pattern text,       -- 版本提取正则；postgres 类写 '^16\.' 锁大版本
  pin_note text,              -- 为什么锁 / 为什么不比上游
  family text,                -- 'own'(自家一致性组) | 'third-party' | 'os'
  sort_order int, active bool
)
version_reports (             -- 上报面，保留历史
  id, device text, image text, tag text, digest text,
  extra_json jsonb,           -- apt 计数、openclaw exec 版本等
  reported_at timestamptz
)
version_upstream_state (      -- 对比面
  component_key text PK, latest_version text, release_url text,
  checked_at timestamptz, check_status text, check_error text
)
```

心跳：`backup_policies` 插三行 `version-inventory-{aliecs,webdock1,webdock2}`（⚠️ 端点校验 policy_code 必须先落库，迁移里插，踩过 404 坑）。

初始组件种子（迁移内 INSERT，来源=2026-07-17 实测容器清单）：

| component_key | 设备 | upstream | 备注 |
|---|---|---|---|
| openclaw | aliecs | github openclaw/openclaw | 版本取 extra_json（exec 采集） |
| authelia / lldap / sing-box | aliecs | 各自 github release | |
| postgres-aliecs | aliecs | dockerhub library/postgres | `^16\.` 锁大版本 |
| immich-server / immich-ml | webdock1 | github immich-app/immich | 痛点组件 |
| immich-postgres / immich-redis | webdock1 | none | pin_note 说明跟随 immich 官方 compose |
| adventurelog-frontend / -backend | webdock1 | github seanmorley15/AdventureLog | |
| gokapi | webdock1 | github forceu/gokapi | |
| aliecs-services / openclaw-bridge / webdock | 多机 | none, family=own | webdock 做两机 tag 一致性 |
| apt-summary | 每机 | none, kind=apt-summary | 只显示数量 |

## 端点（services/backend-api/app/routers/versions.py，按域拆分惯例装配进 main）

- `POST /v1/internal/versions/report`：token 同 `_require_backup_report_token`；body=设备+容器列表+apt 计数+extra；写 version_reports。
- `POST /v1/internal/versions/refresh-upstream`：token 校验；遍历 active 组件查上游（github：`/repos/{ref}/releases/latest`，dockerhub：tags API 按 version_pattern 过滤取最大 semver）；写 version_upstream_state。单组件失败不中断，记 check_error。
- `POST /v1/internal/versions/weekly-digest`：token 校验；汇总落后/security/未登记/缺席（>48h 无上报的设备）→ 飞书直发；全绿发一行短讯。
- `GET /v1/ops/versions`：require_admin；按设备分组返回：组件、当前、最新、状态枚举（`current|behind|pinned|unregistered|own-consistent|own-mismatch|stale`）、落后幅度（semver diff 粗算）。

## 看板（admin-ui /health 页新增「版本」区块）

设备分组表格：组件｜当前｜最新｜状态徽标（✅ / 🔴落后 / 📌锁定 / ⚠️未登记 / ⚪自家一致 / 🟠自家不一致）；apt 行"可升级 N（security M）"；顶部汇总徽标进现有 /health 总览。

## 周报（飞书）

- backend 直发：飞书 `im/v1/messages`，`receive_id` 来自新 env `VERSION_DIGEST_FEISHU_RECEIVE_ID`；凭据复用 sops 中现有 FEISHU_APP_ID/SECRET（新增到 backend 的 env 渲染）。
- ⚠️ backend 加 env 三处接线：sops `aliecs.enc.env` → release-meta.env 渲染 → deploy.sh heredoc → compose 映射（2026-07-13 踩过丢键坑，实施计划里逐处列出）。
- 触发：aliecs systemd timer 每周一 09:00 CST curl 内部端点（与 refresh-upstream timer 同模式）。digest 前先跑一次 refresh-upstream 保证数据新鲜。

## infra 侧（另一个 infra PR）

- `versions/collect-versions.sh`（POSIX sh，jq 组包）+ 三机 timer/service unit + render.sh 接线。
- webdock2 跑在 WSL systemd 内（与 console-ecs-tunnel 同宿主环境）。
- timer 错峰：aliecs 05:00 / webdock1 05:10 / webdock2 05:20（北京）。
- refresh-upstream timer：每日 06:00；weekly-digest timer：周一 09:00。

## 安全

- 上报/内部端点全部 token 校验（复用 backup report token，已在各机 render 链路），不新开公网 location（走现有 `/api/` 前缀 + nginx 既有转发）。
- 上游查询仅出站 HTTPS（github.com / hub.docker.com），无认证信息。
- 不触碰任何密钥值；FEISHU 凭据经 sops 标准流程进 backend env。

## 测试与验证

- pytest：report 端点（匹配/未登记/坏 token）、upstream 解析（mock GitHub/DockerHub 响应、version_pattern 锁大版本、semver 比较）、digest 文案（有异常/全绿两态）、GET 聚合状态机。
- 上线验证：三机手动跑一次采集脚本 → 看板出全量数据 → 手动 curl weekly-digest → 收到飞书消息 → 等一个自然周期确认 timer。

## 上线顺序

1. AliECS PR：迁移 + 端点 + 看板 + digest（release-deploy 自动上线，含 env 三处接线）。
2. sops 补 backend 的 FEISHU 键 + VERSION_DIGEST_FEISHU_RECEIVE_ID → render → 重建 backend。
3. infra PR：采集脚本 + timer；devbox push device-* → 各机 pull + render + enable timer。
4. 三机手动首采验证 → 周一等首份周报。

## 已知风险

- Docker Hub tags API 翻页与乱 tag（`latest`、日期、rc）：version_pattern 白名单正则兜底；解析失败显示 check_error 而非误报。
- GitHub API 60 次/时未认证配额：组件数 ~10，每日一查，余量充足；若将来不够再加 PAT。
- 周报通道依赖飞书可用性：发送失败记日志并在看板顶部显示"上次周报发送失败"。
