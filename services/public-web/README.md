# public-web

Static public pages for AliECS. The homepage shows business feature entries and auth controls. `/formula/` is the system formula page for BOM query, manual BOM sync, workbook download, and formula cost calculation. `/health/` renders ops status, reconciliation details, and external host checks.

## Runtime Inputs

- Browser local storage auth token keys: `aliecs_auth_token`, `portal_token`, `admin_token`.
- Backend API at `/api` in production or `http://localhost:8000` when served on local port `8080`.
- Feature list from `GET /v1/features?include_all=true`.
- Ops status from `GET /v1/ops/status`, host refresh from `GET /v1/ops/hosts/{host_name}/refresh`, and reconciliation detail/actions for `/health/`.
- Recipe query from `POST /v1/recipes/query`, cost calculation from `POST /v1/recipes/cost`, and downloads from `GET /v1/recipes/download/{file_id}`.
- Manual recipe sync from `POST /v1/recipes/sync-bom`; this only requests T+ BOM sync, including disabled BOM rows.

## Runtime Outputs

- Browser-rendered navigation and query previews.
- User-triggered Excel downloads from backend-api.
- User-triggered T+ BOM sync request files created by backend-api for `tplus-sync-worker` to consume.

## Do Not Commit

Do not commit downloaded workbooks, screenshots containing private data, browser storage, copied tokens, or embedded production API secrets.

## Validation

```powershell
docker compose -f AliECS\local\docker-compose.local.yml config
```

After frontend edits, also verify browser behavior against `http://localhost:8080` when the local stack is running. At minimum check page load, login button/modal, `/formula/`, manual BOM sync button, cost table interactions, `/health/`, host detail refresh, and no console syntax errors.
