# doc-sync-worker

Worker-only service for syncing external document data into AliECS Postgres. It must not run inside `backend-api` startup.

## Responsibilities

- Full sync WeCom smart sheets by profile.
- Full sync Feishu bitables by profile.
- Consume manual sync requests created by backend/admin UI.
- Persist fields, normalized records, source metadata, and sync run diagnostics.

## Runtime Inputs

- `DATABASE_URL`.
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
