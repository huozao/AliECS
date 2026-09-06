# public-web

Static public pages for AliECS. The homepage shows business feature entries and auth controls. `/formula/` is the system formula page for BOM query, manual BOM sync, workbook download, and formula cost calculation. `/health/` renders ops status, reconciliation details, and external host checks. `/market/` is the read-only gold cross-market dashboard shell; production data is supplied later by `/api/v1/market/snapshot` and is never read directly from browser-visible files.

## Runtime Inputs

- Browser local storage auth token keys: `aliecs_auth_token`, `portal_token`, `admin_token`.
- Backend API at `/api` in production or `http://localhost:8000` when served on local port `8080`.
- Market dashboard at `/market/`; it reads the authenticated, read-only `/api/v1/market/snapshot` contract. The backend reads the server-side `MARKET_SNAPSHOT_FILE` publication, never browser-visible raw tick files.
- Feature list from `GET /v1/features?include_all=true`.
- Ops status from `GET /v1/ops/status`, host refresh from `GET /v1/ops/hosts/{host_name}/refresh`, and reconciliation detail/actions for `/health/`.
- Recipe query from `POST /v1/recipes/query`, cost calculation from `POST /v1/recipes/cost`, and downloads from `GET /v1/recipes/download/{file_id}`.
- Manual recipe sync from `POST /v1/recipes/sync-bom`; this only requests T+ BOM sync, including disabled BOM rows.

## Runtime Outputs

- Browser-rendered navigation and query previews.
- User-triggered Excel downloads from backend-api.
- User-triggered T+ BOM sync request files created by backend-api for `tplus-sync-worker` to consume.

## Shared Scripts

- `common/user-badge.js` — `AliECSAuth` helpers plus the "current user" badge.
- `common/toast.js` — `AliECSToast.show(text, type)` / `AliECSToast.hide()`, the single implementation of user-facing messages. Pages must not render their own inline message banner: the banner sits at the top of the document and is invisible once the user scrolls down. `error` toasts stay until dismissed; other types auto-hide after 6s. `services/admin-ui/common/toast.js` is a byte-identical copy because admin-ui builds from its own context; `tests/test_frontend_toast.py` asserts both copies match and that every messaging page loads the script.

## Do Not Commit

Do not commit downloaded workbooks, screenshots containing private data, browser storage, copied tokens, or embedded production API secrets.

## Validation

```powershell
docker compose -f AliECS\local\docker-compose.local.yml config
```

After frontend edits, also verify browser behavior against `http://localhost:8080` when the local stack is running. At minimum check page load, login button/modal, `/formula/`, manual BOM sync button, cost table interactions, `/health/`, host detail refresh, and no console syntax errors.
