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

<!-- 本文点名的符号，改名时本文必须同批更新；校验器会拦 -->
<!-- nav-check-python: services/doc-sync-worker/app/pipelines/rnd_record_writer.py:build_node_row_values -->
<!-- nav-check-python: services/doc-sync-worker/app/pipelines/document_locator_mirror.py:write_locator_mirror -->
<!-- nav-check-python: services/tplus-sync-worker/src/tplus_datahub/jobs/db_sync_requests.py:finish_bom_request -->
<!-- nav-check-python: services/tplus-sync-worker/src/tplus_datahub/jobs/sync_state.py:record_tplus_sync_run_if_configured -->
