# AliECS AI Project Map

AliECS is the server-side business platform in the `AliECS-WebDock` workspace. Do not modify `webdock` unless the task explicitly asks for WebDock, browser login, noVNC, or old-laptop automation work.

## services/backend-api

FastAPI API service. Owns auth/RBAC, admin APIs, webhook gateway, recipe query, and workbook downloads.

Runtime inputs: Postgres, runtime env, Chanjet webhook events, and read-only T+ worker output mounted under `/app/tplus-output`.

Runtime outputs: API responses, temporary recipe query workbooks, webhook event spool files, and Postgres writes.

Do not commit: real env files, tokens, secrets, generated workbooks, event spool files, logs, or business data.

Validation: `python -m unittest AliECS.tests.test_backend_recipes` and backend compile checks.

## services/doc-sync-worker

Worker-only sync for WeCom smart sheets and Feishu bitables. It calls external APIs and writes normalized data to Postgres.

Runtime inputs: `WECOM_*`, `WEDOC_*`, `SMARTSHEET_*`, and `FEISHU_*` env vars.

Runtime outputs: `external_sources`, `external_fields`, `external_records`, `sync_runs`, and `sync_requests` updates in Postgres.

Do not commit: WeCom secrets, Feishu app secrets, app tokens, tenant tokens, table data, logs, or local env files.

Validation: `python -m unittest AliECS.tests.test_doc_sync_worker`.

## services/tplus-sync-worker

Worker-only read sync for Chanjet/T+ OpenAPI. It writes raw JSON and Excel output.

Runtime inputs: `CHANJET_APP_KEY`, `CHANJET_APP_SECRET`, `CHANJET_OPEN_TOKEN`, and sync interval env vars.

Runtime outputs: raw JSON under runtime `data/` and workbooks under runtime `output/`.

Do not commit: `.env`, AppSecret, openToken, raw JSON, generated Excel, logs, or cache folders.

Validation: run compileall and unittest from the worker folder with `PYTHONPATH=src`.

## services/public-web

Public homepage and business entry UI. It shows feature cards, login/register actions, and the recipe query panel.

Runtime inputs: browser local storage auth token and backend API responses.

Runtime outputs: downloaded recipe query workbook files in the browser.

Do not commit: generated downloads, screenshots used only for manual QA, or embedded secrets.

Validation: static JS syntax check plus browser smoke against `http://localhost:8080` when the local stack is running.

## services/admin-ui

Admin UI for users, roles, features, doc-sync status, and manual sync requests.

Runtime inputs: admin auth token and backend admin APIs.

Runtime outputs: admin API mutations such as sync request creation and user/role changes.

Do not commit: session tokens, screenshots containing credentials, or production data exports.

Validation: browser smoke after UI changes and backend admin API tests when related routes change.

## deploy/ecs

Production compose and deployment scripts for ECS. Default ECS path is `/root/AliECS`.

Runtime inputs: image tags, private runtime env, Docker volumes, and Postgres.

Runtime outputs: running containers, deployment metadata, and health check logs.

Do not commit: `runtime.env`, `release-meta.env` with real values, private keys, or production logs.

Validation: `docker compose --env-file deploy/ecs/runtime.env.example -f deploy/ecs/compose.prod.yml config`.

## local

Local Docker Compose and local env examples.

Runtime inputs: ignored `local/.env.local` for local testing only.

Runtime outputs: local containers and bind-mounted development data.

Do not commit: `local/.env.local`, local logs, or copied production env values.

Validation: `docker compose -f local/docker-compose.local.yml config`.
