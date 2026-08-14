# 统一同步中心定位档案与旧功能迁移设计（2026-08-14）

> 状态：用户已确认。本文承接 P5，解决文档定位信息持久保存、结构备份可读性、导出/创建副本迁移和企微 A 数量差异。若与 P5 设计冲突，以本文为准。

## 1. 已确认事实

- P5 已部署；生产表级来源与统一作业目录双向差集为 0，手动全量的 22 个文档请求和 1 个 T+ 请求全部成功。
- `peifangpaichan` 的两份 registry 合计 14 个唯一文档：13 个持有有效企微 API `dc` docid，均已在生产登记并建立同步作业；另 1 个“产品名称命名”只有 `s3_` 分享标识，不能调用企微 API，也没有同名有效 docid 可关联。
- `/exports/` 的企微 A 为 13 个，而 P5 `/sync/` 为 12 个：多出的 1 个是专用的“企微智能表格结构备份”文档，其类型为 `structure_backup_doc`，被导出目录统计但被普通同步资产查询排除。
- 当前结构备份按文档展开工作表和字段结构，可读性差；普通手动同步成功不会立即排队结构备份，只有每日全量和 `copy-auto` 会触发。

## 2. 目标与边界

### 2.1 目标

1. 在生产 PostgreSQL 内建立独立的文档定位档案，保存恢复同步连接所需的关键定位和权限信息。
2. 将“企微智能表格结构备份”改为定位档案的人类可读镜像，不再复制逐表逐字段结构。
3. 每次登记、改名、补录 docid、权限验证、同步成功、创建副本或停用后，立即更新档案并排队刷新企微镜像。
4. 把 `/exports/` 的下载与原创建副本能力迁到 `/sync/`；旧页面仅作兼容跳转。
5. 让 `/sync/` 与导出目录使用同一资产集合；结构备份文档可见、可下载，但不得递归同步或创建自身副本。

### 2.2 安全边界

- AliECS 是公开仓库：迁移只提交表结构、代码和虚构 fixture；任何真实 docid、`s3_` 标识、管理员 userid、群 ID 或凭据值不得入仓。
- 真实定位数据只进入生产 PostgreSQL和企微 A 的受控备份文档，并随现有 PostgreSQL 加密备份保存。
- 定位档案只保存凭据的逻辑引用（如企业配置/credential label），不复制 corp secret；真实密钥继续由 infra SOPS 管理。
- 普通资产/overview API 继续不返回真实外部 ID。只有管理员提交补录值，服务端响应也不回显。
- `s3_` 不能伪装成有效 docid。“产品名称命名”保持明确的 `unresolved` 状态，直到人工取得真实 `dc` docid；不得猜测、按名称关联或创建无效请求。

## 3. 主数据架构

```text
私有 registry / 创建副本 / doc-sync 发现 / 管理员补录
                         |
                         v
生产 PostgreSQL document_locator_registry（主数据）
       |                 |                  |
       |                 |                  +-> external_sources / sync_jobs 关联与对账
       |                 +-> 现有 PostgreSQL 加密备份
       v
locator_mirror_jobs（持久任务、幂等、可重试）
       |
       v
企微 A「企微智能表格结构备份」
  - 文档定位档案
  - 定位档案变更历史
```

PostgreSQL 是机器执行的唯一主数据；企微文档是面向人的异地镜像，不反向驱动生产。两者通过稳定 `locator_key` 对应，任何镜像失败都不得回滚已成功的来源同步，但必须保留重试任务并进入告警。

## 4. 数据模型

### 4.1 `document_locator_registry`

一份外部文档一行，核心字段如下：

| 字段 | 语义 |
|---|---|
| `id` | 内部数值主键 |
| `provider` | `wecom` / `feishu` |
| `env_profile` | `COMPANY_A` / `COMPANY_B` / 飞书配置名 |
| `api_doc_id` | 真实 API 定位 ID；unresolved 行必须为空，不得混入分享标识 |
| `share_ref` | `s3_` 分享标识或飞书分享引用；仅作人工定位，永不传给同步 provider |
| `document_name` | 当前实时名称；名称不是主键 |
| `source_url` | 分享/网页地址（如已知） |
| `admin_userids` | 私有管理员 userid 列表 |
| `credential_ref` | 逻辑凭据标签，不含 secret |
| `source_kind` | registry / discovered / copy-auto / manual / system-backup |
| `lifecycle_status` | active / disabled / unresolved |
| `syncability_status` | verified / unverified / invalid-id / permission-denied |
| `can_read` / `can_write` / `can_copy` | 最近一次已验证能力；未知与 false 必须区分 |
| `sheet_count` | 最近一次发现的工作表数量 |
| `external_source_id` | 对应 doc 级 `external_sources.id`；允许 unresolved 行为空 |
| `registered_at` / `last_verified_at` / `last_sync_at` / `updated_at` | 生命周期时间 |
| `last_error_code` / `last_error_summary` | 脱敏后的最近定位/权限错误 |

有效档案按 provider、企业配置和非空 `api_doc_id` 建唯一约束；unresolved 档案按 provider、企业配置和非空 `share_ref` 建唯一约束。补录 `dc` 时在一个事务中锁定 unresolved 行、更新/合并 `external_sources`、重建表级作业目录并记录历史，避免同时保留 s3 与 dc 两个活动文档。

### 4.2 `document_locator_events`

追加式变更历史，保存 locator ID、事件类型、触发来源、变更字段名、脱敏前后状态摘要、操作者和发生时间。不得保存密钥或把完整 docid写进日志文本；docid只存在受控字段。

### 4.3 `document_locator_mirror_jobs`

持久化镜像任务，按 locator 版本/事件去重，状态为 pending/running/success/failed，带 attempt、next_attempt_at 和脱敏错误。worker 用 `FOR UPDATE SKIP LOCKED` 抢占，成功后才标记完成；失败指数退避。

## 5. 私有 registry 导入与持续更新

- 提供生产侧 opt-in 导入命令，从 stdin 接收私有 JSON，不把数据写入仓库、Actions artifact 或日志。
- 首次导入合并两份 registry：有效的 13 个 `dc` 行关联现有生产文档；唯一的 `s3_` 行登记为 unresolved，不入同步队列。
- 导入使用 docid 做主键，registry 的名称仅作来源快照；生产实时名称优先。
- 导入结果只输出计数：inserted/updated/linked/unresolved/conflict，不输出真实定位值。
- 此后新发现文档、同步成功、改名、停用和权限错误均由 worker 在同一业务事务后更新定位档案；不要求生产自动提交 private Git 仓库。
- 创建副本成功后，在返回 API 之前完成“副本文档定位档案 + doc 级来源 + 首次同步请求 + 镜像任务”的单事务登记；外部文档已创建但本地事务失败时，返回明确的 recovery token/脱敏名称，并允许幂等重试登记。

## 6. 企微镜像重构

在现有“企微智能表格结构备份”文档内新增并维护两张权威表：

### 6.1 `文档定位档案`

一份文档一行，与生产定位档案的关键元素一致：

- 平台、企业配置、文档名称、文档定位 ID、来源链接；
- 管理员、凭据引用、来源类型、生命周期状态、可同步状态/原因；
- 可读、可写、可创建副本的已验证状态；
- 工作表数量、登记时间、最后验证时间、最后同步时间、最后更新时间；
- 稳定唯一键和脱敏错误摘要。

### 6.2 `定位档案变更历史`

每次新增、改名、权限变化、补录 docid、创建副本、停用或恢复时追加一行，包含事件时间、文档名称、事件类型、触发来源、变更字段和状态摘要。

### 6.3 旧结构表处理

旧的“企微A-最新结构”“企微B-最新结构”“飞书-最新结构”“结构变更历史”停止写入。由于生产 provider 对删表/改名能力不稳定，本轮不自动删除：保留为只读历史，新的两张表是唯一有效视图。若以后确认 API 删除能力或人工清理，再单独执行，不把破坏性清理混入迁移。

## 7. 触发时序

以下动作成功后必须立即 upsert 定位档案并 enqueue mirror job：

1. 私有 registry 导入或管理员补录；
2. doc worker 发现新文档、改名、工作表数量变化或权限状态变化；
3. 任意整簿手动请求成功，不再只限 `copy-auto`；
4. 定时全量同步完成；
5. 创建副本登记完成；
6. 来源停用、恢复或 docid 合并。

镜像写入使用 locator 版本幂等；连续变化可以合并“当前档案”更新，但每个语义变化都保留历史事件。镜像失败不得阻断同步，pending/failed 必须由 notifier 检测。

## 8. `/sync/` 功能迁移

### 8.1 同步资产

资产 API 与导出 catalog 使用同一聚合 service，并给每项返回：下载是否可用、复制是否可用、同步是否可用及脱敏原因。响应只给内部 `source_id` 和后端下载/动作 URL，不返回外部 ID。

每个资产按能力显示：

- `下载`：沿用现有 T+ 文件和整簿 XLSX 下载端点；
- `立即同步`：仅有效 doc 锚点或 T+ full；
- `创建副本`：仅有效、可读且允许复制的企微普通文档；
- 系统结构备份文档：可下载，标记“系统维护”，不允许普通同步或创建副本；
- unresolved 文档：可见并显示“缺少有效企微 docid”，不创建请求。

企微 A 的资产数量因此与导出目录一致为动态 13（不得在代码或测试写死生产数量）。

### 8.2 创建副本

复用现有 copy provider，但入口迁到 `/sync/`，并强化以下事务/恢复语义：

1. 创建远端副本；
2. 登记定位档案和 doc 来源；
3. 创建首次同步请求；
4. 创建定位镜像任务；
5. 后端响应不返回 docid，只返回内部 source/request ID、名称和状态。

前端必须有 latest-session/request guard，防止退出后或重复点击提交旧响应。后端按副本 recovery key 幂等，避免远端已成功后重试再次创建副本。

### 8.3 兼容入口

- `/exports/` 301 到 `/sync/?view=assets`，不再维护第二套功能 UI。
- `/tplus-sync/` 使用相对 301 `/sync/?group=tplus`，禁止生成 `http://` 降级地址。
- 原 exports 下载、同步和 copy API 暂保留为兼容别名，全部委托统一 service；新页面只调用 `/v1/sync/...` canonical API。

## 9. “产品名称命名”未解析项

该文档当前只有 `s3_` 分享标识，两个私有 registry、生产库和同名生产来源中均没有有效 `dc` docid。系统必须：

- 在定位档案和 `/sync/` 中持续显示 unresolved 原因；
- 禁止按名称猜测、禁止调用企微 API、禁止伪造成功；
- 提供受管理员保护的补录动作，接收真实 `dc` docid 后做只读 `get_doc_name/get_sheets` 验证；
- 只有验证成功且企业配置唯一匹配时才关联来源、创建作业、触发首次同步和镜像。

因此本轮可以完成所有可自动化的迁移，但该行转为可同步仍以取得真实 `dc` docid 为外部前提。

## 10. 错误处理与告警

- registry 导入、补录和 copy 本地登记使用事务；失败回滚。
- 外部 copy 已成功但本地登记失败必须给出可恢复状态，不能让用户盲目重试创建第二份。
- 权限探测只做无副作用读取；`can_write` 只有实际受控写入成功或明确权限 API 证据时才标记 verified，不以“能读取”推断可写。
- 定位镜像 job 持续失败、unresolved 数量增长、有效文档无 source/job 关联均进入同步告警。
- 错误响应、日志、告警和 PR 证据只显示内部 ID、文档名称、错误类型和计数，不显示真实 docid、分享 ID、userid 或 secret。

## 11. 测试与生产验收

### 11.1 自动化

- migration/SQL：唯一约束、unresolved→verified 合并、事件/任务幂等、事务回滚。
- backend：资产下载/copy 能力、系统备份文档可见不可递归、补录校验、copy 幂等恢复、响应无 ID 泄漏。
- worker：任意成功请求立即更新档案并排镜像；改名/数量/权限变化生成事件；失败重试不阻断同步。
- mirror：两张新表的字段与档案一致、旧四表不再写、当前行 upsert、历史 append、敏感日志清洁。
- frontend strict Node：下载、复制、同步、重复点击、乱序响应、logout、unresolved 和系统资产状态。
- PostgreSQL 16 integration：真实迁移、registry 脱敏 fixture 导入、source/job 双向覆盖、copy 登记事务、mirror job savepoint 与清理。
- root/T+ 全套、navigation、Compose、nginx、diff 和秘密扫描。

### 11.2 生产验收

1. 导入两份 registry 后只报告 14/13/1 等计数，不输出定位值；13 个有效项全部 linked，1 个 unresolved。
2. `/sync/` 四类资产与统一 catalog 动态一致；企微 A 包含系统结构备份项，下载可用。
3. 新两张企微镜像表存在，当前档案数量与生产 registry 一致，旧四表在部署后不再新增记录。
4. 触发一次普通文档同步，验证同轮产生定位档案更新时间和成功镜像 job。
5. 选择一份非系统企微文档创建副本一次，验证新档案、source、作业、首次请求、下载和变更历史全部收敛；不得在证据中公开 docid。
6. eligible 表级来源与 enabled 作业双向差集为 0；无 link/system 来源进入普通请求。
7. `/exports/` 和 `/tplus-sync/` 都只返回 HTTPS 安全的相对跳转；`/sync/` 页面无外部 ID 泄漏。
8. doc/T+ 调度继续为 `shadow`，本轮不切 active。

## 12. 回滚

- 新表为附加主数据，不删除 `external_sources`、记录、请求或历史作业；回滚应用镜像后可保留。
- mirror worker 回滚时停止新任务消费，pending job 保留；旧结构表仍在，不丢历史。
- public-web 回滚可恢复旧页面；兼容 API 保留使旧入口不立即失效。
- 若 copy 新流程失败，只回滚本地未提交事务；已经创建的远端副本按 recovery key重新登记，不自动删除远端文档。
