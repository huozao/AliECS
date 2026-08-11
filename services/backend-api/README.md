# backend-api

FastAPI service for AliECS. This is the public API boundary for authentication, RBAC, admin operations, webhook gateway, doc-sync queries, recipe query, and downloadable workbooks.

## Runtime Inputs

- Postgres through `DATABASE_URL`.
- Auth and bootstrap env vars such as `AUTH_TOKEN_SECRET` and `ADMIN_BOOTSTRAP_*`.
- Chanjet webhook env vars such as `CHANJET_APP_KEY`, `CHANJET_APP_SECRET`, and `CHANJET_WEBHOOK_AES_KEY`.
- T+ worker output mounted read-only at `RECIPE_BOM_INPUT_DIR`, default `/app/tplus-output/excel`.
- `CLASH_SELF_NODES_JSON` for the Clash profile composer: a JSON array of complete clash proxy
  definitions, each requiring at least `name` and `server`. Managed in SOPS and rendered at deploy
  time — never commit real node parameters, this repository is public. When the variable is
  missing, malformed, empty, or holds an element without `name`/`server`, both
  `/v1/admin/clash-profile/preview` and `/download` return 500 with an explanatory message rather
  than emitting a profile that would fail to start on the client.

## Runtime Outputs

- API responses for public-web and admin-ui.
- Clash profile composer under `/v1/admin/clash-profile` (all admin-only): `GET/POST /providers`,
  `PUT/DELETE /providers/{id}` manage third-party subscription sources stored in
  `clash_profile_providers`; `GET /preview` returns the composed profile as plain text and
  `GET /download` returns it as an attachment. The service never contacts those subscriptions —
  the client's mihomo fetches them itself through `proxy-providers`.
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
