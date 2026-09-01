# doc-sync-worker

Worker-only service for syncing external document data into AliECS Postgres. It must not run inside `backend-api` startup.

## Responsibilities

- Full sync WeCom smart sheets by profile.
- Full sync Feishu bitables by profile.
- Consume manual sync requests created by backend/admin UI.
- Persist fields, normalized records, source metadata, and sync run diagnostics.
- Watch for a missing aliecs traffic heartbeat and raise an alert when the device
  stops reporting (see below) — this worker is the only place that can, because the
  device itself cannot report that it has gone silent.

## aliecs 流量心跳看护

aliecs 每天往 `notify_outbox` 发一条流量日报（`source_key='aliecs-traffic'`）。
这个 worker 在 `_poll_once` 里（**不是外层 `while True`**——外层一轮是一个完整调度周期）
按小时节流地查一次「最新一条心跳有多旧」，超过阈值就 enqueue 一条 error。

它答的是设备自己答不了的那个问题：**aliecs 越过 200 GB 流量闸门后公网会被限速到约
3.4 KB/s，那时它自身的告警 POST 也发不出去。** 只有接收端看「该来的没来」能发现。

- 开关：`ALIECS_TRAFFIC_HEARTBEAT_MAX_AGE_HOURS`（不设或 ≤0 = 关闭，默认关闭）。
  **打开的前提是已经收到过第一条**，否则采集器没装时会天天报「心跳缺失」。
- 告警自身用 `source='doc-sync'` 写回 outbox。**不能用被监视的那个 source_key**，
  否则这条告警行会把心跳「续上」，自己消掉自己的触发条件。
- 设备侧实现见 infra `roles/server/aliecs-traffic/README.md`；
  中枢侧见 `docs/runbooks/notify.md`。

<!-- nav-check-python: services/doc-sync-worker/app/pipelines/heartbeat_watch.py:check_heartbeat -->
<!-- nav-check-python: services/doc-sync-worker/app/pipelines/heartbeat_watch.py:max_age_hours -->
<!-- nav-check-python: services/doc-sync-worker/app/pipelines/heartbeat_watch.py:ALERT_SOURCE -->

## Runtime Inputs

- `DATABASE_URL`.
- `ALIECS_TRAFFIC_HEARTBEAT_MAX_AGE_HOURS`（可选，默认关闭）、
  `ALIECS_TRAFFIC_HEARTBEAT_SOURCE`、`ALIECS_TRAFFIC_HEARTBEAT_CHECK_INTERVAL_SECONDS`。
- WeCom variables: `WECOM_ENV_PROFILES`, `WECOM_<PROFILE>_CORP_ID`, `WECOM_<PROFILE>_APP_SECRET`, `WEDOC_*`, `SMARTSHEET_*`.
- Feishu variables: `FEISHU_ENV_PROFILES`, `FEISHU_<PROFILE>_APP_ID`, `FEISHU_<PROFILE>_APP_SECRET`, `FEISHU_<PROFILE>_APP_TOKEN`, `FEISHU_<PROFILE>_TABLE_ID`.

## Runtime Outputs

- Postgres tables: `external_sources`, `external_fields`, `external_records`, `sync_runs`, and `sync_requests`.
- Console logs with redacted external API errors.

## Commands

```powershell
python -m app.main sync-wecom-full --profiles COMPANY_A,COMPANY_B
python -m app.main sync-feishu-full --profiles COMPANY_A,COMPANY_B
python -m app.main consume-sync-requests --limit 10
```

Docker local:

```powershell
docker compose -f AliECS\local\docker-compose.local.yml run --rm doc-sync-worker python -m app.main sync-feishu-full --profiles COMPANY_A
```

## Do Not Commit

Do not commit WeCom secrets, Feishu app secrets, app tokens, tenant access tokens, local `.env` files, table data, logs, or API response dumps.

## Validation

```powershell
python -m compileall AliECS\services\doc-sync-worker\app
python -m unittest AliECS.tests.test_doc_sync_worker
```
