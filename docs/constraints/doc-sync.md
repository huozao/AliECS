# Doc Sync 开发约束（改 doc-sync-worker / 同步表结构前必读）

> 原属 AGENTS.md 常驻规则，2026-07-19 指针化迁入此处。约束本身未变。

- 企业微信智能表格同步必须由 `services/doc-sync-worker` 独立执行，不要放进 `backend-api` 启动流程。
- 第一版同步模式默认为 full，必须同步 docid 下所有 sheet，并对每个 sheet 按企业微信分页拉取全部 records。
- 不允许只取最近记录，不允许只同步一个 sheet，不允许只同步字段后跳过 records。
- AliECS 的同步主数据源是 Postgres，不要把 PeiFang 的 `output/latest` 或 `data/wecom` 复制成主数据源。
- backend-api 只能查询同步结果、同步状态和同步日志，不直接调用企业微信 API。
- Admin UI 可以提供"企微或飞书同步"后台核验模块；同步表格的具体数据属于后台数据，不要放到 public-web 首页作为公网入口。
- 手动同步应通过 `sync_requests` 或 worker 命令执行，不要让 backend-api 直接调用企业微信或飞书 API。
- 环境变量示例只写占位值或本地测试值，不提交企业微信真实 corp id、secret、token 或业务数据。
- 修改 doc-sync-worker、同步表结构、backend 查询接口、Docker Compose 或部署配置后，优先运行：

```bash
python -m unittest discover -s tests
docker compose -f local/docker-compose.local.yml config
```

- 如果 Docker 无法运行，必须说明未验证原因，并给出本地补救命令。

## 写企微智能表格：单元格必须是 cell 数组（已坑过两次）

往企微智能表格写数据时，`/wedoc/smartsheet/add_records` 与 `/wedoc/smartsheet/update_records`
的每个单元格**必须是 cell 数组**：

```python
{"字段标题": [{"type": "text", "text": "值"}]}
```

**传裸字符串 `{"字段标题": "值"}` 时接口照样返回 `errcode=0`**，但值不会落库，写进去的是空行。
没有任何报错、任何重试信号——这是本条最危险的地方：任务全部标成功，人却在文档里看到空表。

连锁后果：空行不带业务唯一键，按唯一键做的 upsert 索引不到它，于是每轮都再 append 一行。
症状是**行数持续增长且全为空**，而不是简单的"没写进去"。

两次实际发生：

- 2026-08-14 定位档案镜像上线，两张表各堆 43 条空行（档案实际只有 25 条）。
- 同一写法的旧结构备份「企微A-最新结构」411 行同样是空的，从未真正备份到内容。
  ⚠️ 2026-08-19 复核：不止那一张，四张旧表**全部**是空行——企微A 411 / 企微B 361 /
  飞书 68 / 结构变更历史 337，合计 1177 行、138 列，逐格回读全为空。另外这条流水线
  自 2026-08-14（PR #313）起已无任何调用者，最后一个成功任务停在 08-14 07:51。
  该流水线已于 2026-08-19 正式下线、实现代码删除，`wecom_structure_backup.py` 只留
  企微智能表格的通用读写工具；唯一有效的定位备份是「文档定位档案」两张表。

判定与排查：

- 判据只有一个——**回读单元格文本**，数"非空行数"。作业状态、`errcode`、重试次数全部无效。
- ⚠️ 2026-08-19 起这条判据**已进生产运行时**：`write_locator_mirror` 每次写完立即回读比对，
  非空期望值缺失或值不一致一律抛 `DocumentLocatorMirrorError`，走既有的持久重试与告警，
  不再标成功。此前它只落在测试的 fake 上，生产写入侧一路无人回读，才堆出上面那些空行。
  报错只带字段名、不带值——docid 与分享标识不得进日志文本。
- 读取端 `_cell_text()` 一直在解析 cell 结构，所以读到的空 `values` 是真空，不是解析问题。
- 交叉验证：主同步链路读业务表能拿到内容（`external_records.normalized_json` 非空），
  说明读没问题；能写入的正确样板见 `build_node_row_values`。

写测试时注意：假客户端如果原样存下裸字符串又原样读回，等于替企微"接受"了本该丢弃的数据，
测试会一路绿灯。**fake 必须丢弃非 cell 数组的值**，否则它锁的是错误契约。
`write_locator_mirror` 的回归测试已按此写法钉死。

## 企微文档的定位信息：创建时是唯一一次机会（2026-08-19）

自建应用建出来的文档，**丢了定位方式就等于失联**——registry 里那个只有 `s3_` 分享标识的
「产品名称命名」调不了 API，至今是 `unresolved`，就是失联的样子。相关约束：

- **docid 不等于密钥**。备份文档里只存 `credential_ref`（值就是 `COMPANY_A` 这类标签），
  corp secret 只在 infra SOPS。跨企业拿 docid 调 API 会直接 `301085 invalid docid`（已实测）。
  但拼出来的 `https://doc.weixin.qq.com/smartsheet/<docid>` 能不能打开由**企微侧分享设置**
  决定，与凭据无关——所以「docid 泄露无害」只在 API 路径上成立，不要外推。
- **定位档案是文档级的，同步身份是表级的**。`document_locator_registry` 一份文档一行
  （生产 27 行），而真正驱动同步的是 `external_sources` 的四元组
  (provider, env_profile, external_doc_id, **external_sheet_id**)，`sync_jobs` 靠 `source_id`
  关联它（生产 93 个 active 表级来源）。只镜像文档级等于丢掉 sheet_id 和 source_id，
  恢复时只能靠子表名去猜、子表一改名就错配。故 2026-08-19 新增第三张镜像表
  **「同步表格清单」**，一行一个表级来源，含 API文档ID / 子表ID / 来源ID / 作业键；
  `disabled`、`inactive` 的行同样写入并标状态——停用不等于可以丢身份。
  它挂在**全量同步之后**整表刷新，不跟 mirror job 走：新子表被发现不产生文档级 locator 事件。
- **`api_doc_id` 与 `share_ref` 必须分列**。合成一列会让人从镜像恢复时分不清哪个能调 API、
  哪个只能人工找。镜像里「文档定位ID」保留只作速览，权威值看「API文档ID」「分享标识」。
- **`create_doc` 的应答要留住**。应答里带 `url`，此前只取 `docid`、链接一律客户端拼接，
  等于把唯一一次拿到权威链接的机会丢了。现在应答链接优先，缺失或不一致都进 warnings。
- **`admin_users` 是传入值，不是回查值**。它决定人能不能在企微客户端里看到这个文档；传空
  就意味着该文档只剩 API 可达。企微是否提供查询文档管理员的接口**尚未确认**，所以档案里这
  一列只能当「创建时传入什么」看，不能当已验证事实。传空时 `copy_smartsheet_doc` 会 warn。
- **企微 provider 没有删表/改表名能力**（原 `ensure_backup_workbook` 的注释，随该函数删除搬到这里）：
  `rebuild_sheet_with_order` 那种「建临时表→搬数据→改名→删旧表」的做法在生产不可行，
  触发即在 `add_sheet` 后中途崩溃并把任务卡死在 running。补列只能「只补不删」。
- **`add_fields` 是插在首列之后，不是追加到末尾**，所以 `_initialize_sheet_fields` 每批要
  `reversed`，**批与批之间也要 reversed**。字段数 ≤21 时只有一批、错位不显形，定位档案
  2026-08-19 加到 26 列才暴露。改字段契约时留意这个批次边界。
## 结构备份文档的实际内容与下载口径（2026-08-20）

`WECOM_STRUCTURE_BACKUP_DOCID` 指向的「企微智能表格结构备份」文档，现存子表**只有三张**，
全部是定位档案镜像：`文档定位档案`、`定位档案变更历史`、`同步表格清单`。
⚠️ 08-19 条目里提到的四张旧结构备份表（企微A/企微B/飞书-最新结构、结构变更历史）
**已不在该文档中**；`external_sources` 里对应的 1642-1645 四行是陈旧残留，
2026-08-20 已置 `disabled`。资产页「表数」曾因此显示 4，实际是 3。

- **下载走 DB 实时生成，不读企微文档**（`exports.py` 的 `_append_locator_archive_worksheets`）。
  所以"下载到的不是最新的"有两层原因要分开看：一是 registry 内容取决于上一轮全量
  （每天一次，跳过 `modify_time` 未变的文档），二是导出端可能没跟上镜像端新增的表。
- **镜像写几张表，导出就必须出几张表**。08-19 加了「同步表格清单」，导出端漏跟，
  129 行表级身份从未进入下载文件，直到 08-20 才补齐。改 `INVENTORY_FIELDS` 或新增镜像表时，
  `_LOCATOR_INVENTORY_FIELDS` 与那段 SQL 必须同步改——两边各写一套列，
  从备份恢复时就不知道该信哪份。
- 系统管理资产的「表数」在页面上显示为「—」：它的子表不由同步管道维护，
  显示同步表数只会误导。

## lifecycle_status 是派生值，"放弃一个失联文档"必须改来源（2026-08-20）

`document_locator_registry.lifecycle_status` 不是可以直接写的字段，每轮 reconcile 都由
`document_locator.py` 的 `locator_from_source()` 从 `external_sources` 重新算出来。
直接 UPDATE registry 会被下一轮刷回去。

判定顺序也踩过一次：原来写的是 `unresolved if not resolved else (active if active else disabled)`，
即"未解析"优先。于是把失联来源置 `disabled` 也会被算回 `unresolved`，
人工放弃动作永远落不了地，资产页也永远清不掉那一行。
2026-08-20 起改为**停用优先**：`disabled if not active else (active if resolved else unresolved)`。

放弃一个失联文档的正确动作：把 `external_sources` 对应行 `status` 置 `disabled`，
等下一轮 reconcile 把 registry 刷成 `disabled`，`ASSET_SQL` 的
`lifecycle_status IN ('active','unresolved')` 自然把它过滤掉。**档案行本身保留**——
分享链接和历史还查得到，人工迁移内容时还用得上。

## 同步资产 = 文档级视图（2026-08-20 合并作业总览）

同步的真实粒度就是文档：`_sync_doc()` 先比 `modify_time`，没变整簿跳过，变了才遍历全部子表。
所以页面按文档展示，原「作业总览」已并入「同步资产」。两条判据别再改回去：

- **聚合键是 `doc_source_id`（后端 `_OVERVIEW_SQL` 新增列），不是文档名**。文档一改名，
  按名字聚合就会错配到别的行。
- **孤儿作业的判据是「没有任何资产认领它」，不是写死的 job_key 名单**。名单会随新增系统任务
  悄悄失效，作业从所有分组里一起消失。没被认领的作业进「系统任务」分组。

表级明细默认不展示，**只在该文档有 failed/partial 或未解决告警时自动展开**，且只列问题表。
这一层不能省：`_sync_doc` 的 sheet 级容错让单表失败不拖垮同文档其余表，
失败的表又因为整簿跳过而长期不重试——`产量统计 / 公开的生产记录表` 从 08-13 挂到 08-20，
告警通知了 27 次，文档级视图里只是一个「告警 1」。

## 相关背景（运行现状）

- 生产唯一实例运行在 txecs 的 `business-cn-doc-sync-worker-1`；aliecs 旧实例保持停止。
  如果企微接口返回 `errcode 60020`，按错误中的 `from ip` 核对企微自建应用可信 IP；
  IP 已存在仍报错时，先确认当前后台企业 ID 与失败 profile 的
  `WECOM_<PROFILE>_CORP_ID` 一致，避免在另一企业主体中重复配置。
  当前服务器出口以 `docs/fleet.md` 和实时错误为准，不在本文件复制 IP。
- 同步调度已 DB 化：`system_config` 存调度配置，每天北京时间 02:00 全量（2026-07-06 PR#160/#161）。
- 手动同步/整簿重扫：INSERT `sync_requests` 后由 worker 消费（飞书重扫 2026-07-05 PR#159）。
- 设计文档：`docs/doc-sync-design.md`。
- T+ 父件核对（2026-08-04）：`app/pipelines/tplus_parent_match.py`，每天全量同步后随 `run-loop` 跑一次，
  也可手动 `python -m app.main tplus-parent-match [--dry-run] [--no-notify]`。
  它按 `tplus_bom_records` 中 `missing_since IS NULL` 的记录，把父件名称核对回企微
  「色粉使用记录表 / 标准型号0117」的「父件名称 / T+匹配状态 / T+核对时间 / T+停用」四列。
  **2026-08-11 换源**：原为文档「标准型号0117」/ 表「标准型号规格&月统计」（doc 级 20106、sheet 20120），
  现为文档「色粉使用记录表」/ 表「标准型号0117」（doc 级 20671、sheet 20673）。
  **本仓库是公开仓库，任何企微 docid 都不要写进来**——需要时查 `external_sources.external_doc_id`。
  换源前逐值校验：两边各 323 行、键集合零差异、16 个关键字段值全等，原表 29 个字段一个不缺。
  ⚠️ **新文档里有两个能匹配成功却是错的表**：「标准型号011」（少个 7，同样 323 行 32 字段）、
  「标准型号规格&月统计」（与旧表同名，只有 157 行且缺父件名称/T+* 等 9 个字段）。
  源由 `(document_name, sheet_name)` 定位而非 docid，**写错一个字符会静默切错源**，
  `tests/test_backend_formula_colors.py` 已把正确值和这两个陷阱钉死。
  旧源保持 active 继续同步但不再被任何程序读写，回滚 = 改回两处常量重新部署，数据零损失。
  换源同时要改的是 backend `app/routers/formula_colors.py` 与本管道的两个常量，
  **两者必须同一对名字**，改一个漏一个会让页面和写回指向不同的表。
  2026-08-10 起加了 `T+停用` 列（取值 `停用`/`启用`，源是 T+ 原始行的 `Disabled`，BOM 父件取 BOM 单的、
  纯存货父件取存货档案的）。**停用不参与失联判定**——停用件仍在 T+ 里、编码有效，和「编码失联」
  是两回事，混一列会让人按失联去查一个其实还在的编码。告警只报**本轮新变成停用**的行，
  不报存量（存量每轮都在，报了就是每天一条一模一样的告警）。上线首轮会因为该列原本为空而
  重写全表约 200 行并盖一次核对时间戳，之后回到增量。
  2026-08-10 上线实测：首轮「停用 14」、次轮「停用 0」（存量不复报），全表
  323 行 / 有编码 287 / 一致 287 / 失联 0 / 待补建 0。注意 **14 是表行数不是编码数**——
  T+ 侧按编码去重只有 13 个停用父件，企微表同一父件编码可对应多行型号规格，核对按行计数。**父件编码是执行主键，失联时只标状态、绝不自动改写**；
  异常推 `TPLUS_PARENT_MATCH_CHAT_ID` 指定的飞书群（留空则只写表不推送）。
  2026-08-06 起同一管道还会**补建缺失行**：`tplus_bom_records` 里有、企微表没有的父件编码，
  按「父件编码 / 父件名称 / T+匹配状态=一致 / T+核对时间」四列建行，**型号与 Lab/容差列一律留空**，
  人工按「型号为空」筛出待补标准的行。补建只新增、不改写已有行的编码。
  **人工删掉的行下一轮会被重新建出来**（已确认接受，无删除白名单）。
  同时核对时间改为只在该行内容变化时才写——全量后每轮重写整表既无信息量又吃接口配额。
  首次上线务必先 `--dry-run` 看待补建行数再实跑。
  注意 `tplus_bom_records` 按版本累积，同一编码有多条历史记录，漏掉 `missing_since` 过滤会取到已作废的旧名称。
  触发时机是**每日兜底 + 事件触发**双通道：兜底走 `run-loop` 的全量周期；事件触发在同一 loop 的
  poll 周期（`DOC_SYNC_POLL_SECONDS`，默认 30s）里查 `integration_sync_runs` 中
  `provider='chanjet' AND status='success' AND module IN ('all', 'bom')` 的 `MAX(finished_at)`，
  水位上涨即跑一次核对与补建。水位存 worker 进程内存，重启后首轮只记水位不跑（补建幂等，兜底轮已覆盖）。
  该 SQL 对应 tplus-sync-worker 两个真实写入点：`module='bom'` 是 `db_sync_requests.py` 的
  `finish_bom_request()`（消费 BOM builder 提交回写），`module='all'` 是 `sync_state.py` 的
  `record_tplus_sync_run_if_configured()`（`worker_loop.py` 每日全量以 `module="all"` 调用，
  是直接在 T+ 建物料/BOM 时最常见的来源）。`status='success'` 必须过滤，否则一次部分成功的全量
  会把本批未出现的记录标 `missing_since`，触发核对时把大量行误标「编码失联」发大告警。
  **改那边任一写入点的 provider/module/status 语义就要同步改这里**，否则事件触发会静默失效。

## 企微 get_records 只认 offset，回传 next 会被忽略（2026-08-28）

`WeComSmartsheetClient.get_records` 曾拿响应里的 `next` 当游标翻页。**企微忽略请求里的
`next`**：连发 8 次带 `next=50` 的请求，每次都返回同一批前 50 条、`has_more` 恒为 `true`、
`next` 恒为 `50`——旧写法是个死循环。

它一直没爆，只是因为**不传 `limit` 时企微一次性把整表返回**（生产「生产色粉明细」524 条 /
分页 1 页），`has_more` 永远是 false，翻页分支从来没被走到。

现行写法：显式 `limit=RECORD_PAGE_SIZE`（50），用 `offset` 翻页。判据锁在
`tests/test_doc_sync_worker.py` 的 `_offset_only_client`——那个假客户端**只认 offset、
忽略 next**，就是为了让旧写法在测试里必然失败。

## 个别记录企微拒绝返回：60111，裁字段绕不开（2026-08-28 定案）

`wecom.doc.2｜产量统计 / 公开的生产记录表` 自 2026-08-13 起 `failed`，告警推了 58 次。
**不是 docid 失联**：同一 docid 下的「选单录单」读得通，每晚 `get_doc_base` 也正常。

实测证据（COMPANY_A，只读探针）：

| 请求 | 结果 |
|---|---|
| `offset=0 limit=84` | OK，84 条 |
| `offset=0 limit=85` | ERR 60111 |
| `offset=50 limit=50`（第 51–100 条） | ERR 60111 |
| `offset=84 limit=1`（第 85 条） | ERR -1，连测 3 次全错 |
| `field_ids` 排除「创建人」(`FIELD_TYPE_USER`) | ERR 60111 |
| `field_ids` 只要「创建人」 | ERR 60111 |

`total=146`，读不出的是**第 85 / 86 / 97 条**，其余 143 条正常。这三条的「创建人」指向本企业
解析不到的 userid（离职或外部提交人）。**裁掉成员字段绕不开**——企微在返回前先解析 userid，
`field_ids` 过滤发生在解析之后。

处理口径（2026-08-28 用户拍板「甲」）：

- 整页失败时降级成逐条拉取，跳过读不出的记录，序号回传为 `unreadable_offsets`。
- 运行状态仍是 `success`：这是**数据源的稳定缺陷，不是同步失败**，每天推一次告警没有信息量。
  条数写进 `sync_job_runs.detail_json.unreadable_record_count`，页面上常驻一个「N 条不可读」chip。
- 🔴 **有不可读记录时整轮放弃 `delete_missing_records`**。「本轮没见到」不能推断成「上游已删」，
  照删会把仍然存在、只是这次拉不回来的记录从库里抹掉。

## 整簿跳过必须留痕；半途失败不得登记 modify_time（2026-08-28）

企微 `modify_time` 未变时整簿跳过，旧实现在跳过分支直接 `return`：既不写 `sync_job_runs`
也不动 `last_sync_at`。后果是同步中心页面上「最近运行」停在最后一次**内容有变化**的日子——
2026-08-28 用户据此报「企微 A 和企微 B 都没同步」，实际每晚都在跑。飞书没有跳过机制
（每轮全拉 13 张表），所以飞书天天有记录，对比之下更像企微坏了。

现行口径：

- 跳过时为该文档下每个 active 表级作业写一条 `status='skipped'` 的运行记录
  （`_record_skipped_runs`，来源列表走 `list_active_sheet_sources`）。
- 新鲜度看 **`verified`＝最近一次 `success` 或 `skipped`**，不是 `last_success_at`。
  只认 success 的话，配上 SLA 之后那几十张长期无改动的表会集体变成「已过期」，全是假告警。
- 页面列名「最近同步」改为**「最近取数」**：它读的是 `MAX(external_sources.last_sync_at)`，
  跳过时不更新，本来就只在真拉了数据时才动。
- ⚠️ `sync_wecom_full.py` 里「全簿处理完才登记 modify_time，半途失败下轮不会被跳过」这句注释
  **在 2026-08-28 之前只是注释**：`upsert_doc_source(external_modified_at=...)` 是无条件执行的。
  `wecom.doc.2` 因此从失败那天起被每晚跳过、定时任务从未重试过。现已改为失败时传空串，
  下一轮 `last_seen` 为空就不会命中跳过分支。
- ⚠️ **上一条的修复救不回「已经卡进跳过循环」的文档（2026-08-30 实测）**：清空
  `external_modified_at` 发生在「真的跑了一轮并失败」的路径上，而跳过分支在 `sync_document`
  开头就 `return`，根本走不到那里。`产量统计` 最后一次真跑是 2026-08-29 01:05 CST（`partial`），
  当时 worker 还是旧镜像（`t-075aa2b70e8a` 08-29 22:13 CST 才上线），旧代码无条件登记了
  `modify_time=1787890319`；新代码上线后 08-30 00:32 那轮直接命中跳过分支，于是再也出不来。
  **新代码只对「在新代码下发生的失败」生效，存量卡死的文档必须手工救一次**：清 doc 级来源
  （`external_sources` 里 `source_type='smartsheet_doc'` 的那行）的 `external_modified_at`，
  清完下一轮才会真跑。判别是否卡住：该文档 doc 级行的 `external_modified_at` 非空，
  但表级作业里有 `last_success_at` 为 NULL 或长期不动的。2026-08-30 全库扫过一遍，
  卡住的只有 `产量统计` 一个，已清空 id=10 的 `external_modified_at`。

## `wecom.doc.2` 从 2026-08-13 起失败的根因是 60111（2026-08-30 定案）

`公开的生产记录表` 里序号 85 / 86 / 97 三条记录的成员字段指向解析不到的 userid，
企微对**整页**返回 60111，`fetch_page` 直接抛异常——不是网络、不是凭据、不是分页。
PR#330 已加逐条容错（读不出的跳过、并且本轮跳过删除比对，避免把读不出的记录判成已删除）。

- **修复有效，但一次都没跑到**：容错随 `t-075aa2b70e8a` 于 2026-08-29 22:13 CST 上线，
  而该文档在 2026-08-29 01:05（旧代码）已被登记 modify_time 卡进跳过循环，见上一节。
- 复现与恢复用的是同一条命令，绕开跳过分支直接跑单个表级来源：

  ```bash
  ssh txecs "docker exec business-cn-doc-sync-worker-1 \
    python -m app.main sync-wecom-source --source-id <external_sources.id>"
  ```

  2026-08-30 实测：143 条全部拉回、3 条不可读跳过、`success`，
  那条从 08-13 起通知了 65 次的 `failed` 告警随即自动解除。
- ⚠️ 排查时**不能只看 `sync_job_runs`**：它的 `error_kind='unknown'` / `error_message='sync failure'`
  两个字段对这类失败毫无信息量，告警 `payload_json` 里的 `error_message` 还是 `[REDACTED]`。
  真正定位靠的是 `sync_job_steps`（能看到失败停在 `fetch_page` 且 `message` 是表名）
  加上现场复现。容器重建后旧日志即丢，别指望翻历史日志。

## 新鲜度 SLA 分档（2026-08-30）

92 个作业里 91 个原本没配 `sync_jobs.freshness_sla_seconds`，页面「未监控 91/92」。
2026-08-30 按类型补了 62 个，分档口径如下（改的是生产库 `sync_jobs`，不在代码里）：

| 档 | 对象 | SLA |
|---|---|---|
| 控制面 / 配置表 | 管理面板 3、系统配置 5、飞书 ChatGPT 会话管理台 8、`tplus.parent_match`、`wecom.locator_mirror` | 24h |
| 业务主数据 | 色粉基础数据 10、生产任务排期 9、登记表 9、色粉使用记录表 6、登记表-副本 4、待处理产品记录 2、生产任务统计 2、产量统计 2 | 48h |
| 副本 / 测试 / 低频 | 点餐表×3 共 12、经典语录×3 共 4、案例表×3 共 4、点检表×2 共 4、测试 3、fatfinger 1、标准型号0117 1 | 不配（29 个） |
| 保持不动 | `chanjet.full` | 原有 48h |

补完当场是 **63 新鲜 / 29 未监控 / 0 过期 / 0 预警**，没有触发任何新告警——因为新鲜度认
`skipped`（见上一节）。`chanjet.full` 原方案要从 48h 降到 24h，预演发现降完立刻变过期
（最近确认在 31.6 小时前），故保持 48h 不动。

回滚：把上述 62 个作业的 `freshness_sla_seconds` 置回 NULL 即可，无代码依赖。

⚠️ **`wecom.doc.2`（产量统计 / 公开的生产记录表）的「新鲜」是假的**：它 `last_success_at`
至今为 NULL——从未成功过一次，但调度器每晚给它写 `skipped`，`verified` 因此天天刷新，
配上 48h SLA 后会永远显示新鲜。真正覆盖它的是它自己那条从 2026-08-14 起持续通知的
`failed` 告警，不是新鲜度。根因是上一节的「卡进跳过循环」，不要靠把它排除出 SLA 来绕开。
（2026-08-30 已恢复：手动 `sync-wecom-source --source-id 2` 拉回 143 条并解除告警，
根因见下一节。）

### 补 SLA 前必须先确认「告警器」也认 skipped——页面和告警器是两份独立部署（2026-08-30）

**页面新鲜度（backend-api 的 `sync_read.py`）和告警判据（doc-sync-worker 的
`sync_alert_notifier.py`）是两个服务里的两份代码，部署状态可以不一致。**
2026-08-30 补完 62 个 SLA 后，页面显示 63 新鲜 / 0 过期（backend-api 已含 PR#330 的
`verified` 判据），而 doc-sync-worker 还停在 `t-075aa2b70e8a`——PR#332 合并了但没部署，
告警器仍按 `status = 'success'` 判定，**30 秒内批量发出 41 条 `stale` 告警并实际通知到飞书群**。
按 success-only 判据够得上 stale 的正是那批长期无改动、只有 `skipped` 记录的作业。

动手前的断言（必须在生产容器里查，不是查代码库）：

```bash
ssh txecs "docker exec business-cn-doc-sync-worker-1 \
  grep -n \"WHERE job_id = j.id AND status\" /app/app/pipelines/sync_alert_notifier.py"
# 必须是 status IN ('success', 'skipped')；是 status = 'success' 就先别配 SLA
```

⚠️ **误告警不能靠「在库里标 resolved」收场**：告警器在轮询里**每 30 秒**评估一次
（`_notify_fail_open()` 在 `_poll_once` 里，`DOC_SYNC_POLL_SECONDS=30`），标了 resolved
会在 30 秒后被重新 `claim_alert` 开出来，且新告警 `notify_count=0` 不受 6 小时节流
（`SYNC_ALERT_ESCALATION_SECONDS=21600`）保护，**立刻再发一轮**。唯一正确的顺序是
**先让条件为假**（部署正确判据，或把 SLA 撤回 NULL），再让告警器自己解除。
解除同样会推送（`resolve_alert` 带 sender），所以 41 条告警对应 41 条「同步已恢复」，
这笔噪音在配 SLA 之前就该算进去。

## 「立即同步」的判据只有一个（2026-08-28）

前端曾用 `job.enabled` 自己推导按钮是否渲染，于是 `kind='mirror'` 的
`wecom.locator_mirror｜企微文档定位档案镜像` 和 `kind='reconcile'` 的
`tplus.parent_match｜T+ 父件核对` 也长出了「立即同步」，点下去必然报
**「同步作业不存在或不可手动触发」**——`enqueue_doc_job` 要求 `kind='pull'` 且有有效表级来源。

判据现在只有一份：`sync_control.manual_triggerable(job_key, kind, source_id)`，
由 `/v1/sync/overview` 的 items 带给前端。`chanjet.full` 例外放行，因为路由对它单独分派到
`enqueue_tplus_full`。**前端不得再自己推导这件事。**

`wecom.locator_mirror` 另有一处：它走 `document_locator_mirror_jobs` 独立队列，此前从不写
`sync_job_runs`，登记进 `sync_jobs` 只是为了让告警器认它——所以页面上永远显示「无记录」。
现已在 `run_pending_document_locator_mirror_jobs` 每一轮起止各写一条运行记录。

## 运行记录保留策略（2026-08-28）

整簿跳过留痕后 `sync_job_runs` 写入量从约 33 行/天涨到约 90 行/天。清理挂在每日全量之后
（`worker_loop._default_full_sync`，循环里唯一天然的「一天一次」入口，不引入 pg_cron）：

- 删 `started_at < now() - 90 天`（`RUN_RETENTION_DAYS`）
- 🔴 **但每个作业保底留最近 5 条**（`RUN_RETENTION_MIN_PER_JOB`）。纯按时间删会把低频作业
  删成「无记录」——那正是本次在修的、最容易被误读成「这个作业坏了」的状态。
- `sync_job_steps` 走 `ON DELETE CASCADE` 一起清；`sync_job_alerts.run_id` 是
  `ON DELETE SET NULL`，告警本身不受影响。

<!-- 本文点名的符号，改名时本文必须同批更新；校验器会拦 -->
<!-- nav-check-python: services/doc-sync-worker/app/pipelines/rnd_record_writer.py:build_node_row_values -->
<!-- nav-check-python: services/doc-sync-worker/app/pipelines/document_locator_mirror.py:write_locator_mirror -->
<!-- nav-check-python: services/doc-sync-worker/app/providers/wecom.py:get_records -->
<!-- nav-check-python: services/doc-sync-worker/app/pipelines/sync_wecom_full.py:_record_skipped_runs -->
<!-- nav-check-python: services/doc-sync-worker/app/storage/postgres.py:prune_sync_job_runs -->
<!-- nav-check-python: services/backend-api/app/sync_control.py:manual_triggerable -->
<!-- nav-check-python: services/tplus-sync-worker/src/tplus_datahub/jobs/db_sync_requests.py:finish_bom_request -->
<!-- nav-check-python: services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py:record_tplus_sync_run_if_configured -->
