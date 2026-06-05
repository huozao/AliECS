# backend-api

FastAPI service for AliECS. This is the public API boundary for authentication, RBAC, admin operations, webhook gateway, doc-sync queries, recipe query, and downloadable workbooks.

## Runtime Inputs

- Postgres through `DATABASE_URL`.
- Auth and bootstrap env vars such as `AUTH_TOKEN_SECRET` and `ADMIN_BOOTSTRAP_*`.
- Chanjet webhook env vars such as `CHANJET_APP_KEY`, `CHANJET_APP_SECRET`, and `CHANJET_WEBHOOK_AES_KEY`.
- T+ worker output mounted read-only at `RECIPE_BOM_INPUT_DIR`, default `/app/tplus-output/excel`.

## Runtime Outputs

- API responses for public-web and admin-ui.
- Postgres writes for auth/RBAC, features, sync requests, webhook state, and admin operations.
- Temporary recipe query workbooks under `RECIPE_EXPORT_DIR`.
- Chanjet event spool files under `CHANJET_EVENT_SPOOL_DIR`.

## Do Not Commit

- Real `.env` files, runtime env files, tokens, secrets, webhook tickets, generated workbooks, spool files, logs, or business data.
- Absolute local paths from one machine unless they are only used in tests.

## Validation

```powershell
python -m compileall AliECS\services\backend-api\app
python -m unittest AliECS.tests.test_backend_recipes
```

When Docker is available:

```powershell
docker compose -f AliECS\local\docker-compose.local.yml config
curl.exe -fsS http://127.0.0.1:8000/healthz
```
