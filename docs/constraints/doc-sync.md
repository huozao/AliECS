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
  它按 `tplus_bom_records` 中 `missing_since IS NULL` 的记录，把父件名称核对回企微「标准型号0117」的
  「父件名称 / T+匹配状态 / T+核对时间」三列。**父件编码是执行主键，失联时只标状态、绝不自动改写**；
  异常推 `TPLUS_PARENT_MATCH_CHAT_ID` 指定的飞书群（留空则只写表不推送）。
  注意 `tplus_bom_records` 按版本累积，同一编码有多条历史记录，漏掉 `missing_since` 过滤会取到已作废的旧名称。
