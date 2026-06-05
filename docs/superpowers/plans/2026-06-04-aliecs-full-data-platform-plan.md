# AliECS Full Data Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build AliECS into a server-hosted, read-first business data platform that continuously syncs T+ / Chanjet, WeCom smart sheets, and Feishu bitables, then exposes recipe query and downloadable review workbooks on the public homepage.

**Architecture:** Keep public ingress, auth, query APIs, and downloads in `services/backend-api`; keep public navigation in `services/public-web`; keep external API pulling in independent workers (`services/tplus-sync-worker` and `services/doc-sync-worker`). All secrets stay in runtime `.env` or ECS private env files, not Git. Runtime business data lives in Postgres, Docker volumes, or ignored output folders.

**Tech Stack:** Python 3.12, FastAPI, Postgres, Docker Compose, vanilla HTML/JS public/admin UI, pandas/openpyxl for recipe workbook generation, GitHub Actions deploy to ECS.

---

## Current State Snapshot

Repository: `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS`

Existing major services:

- `services/tplus-sync-worker`: Chanjet/T+ read-only worker with BOM, inventory, partner and other QueryPage sync modules. Output is runtime data under `data/` and `output/`.
- `services/doc-sync-worker`: WeCom smart sheet sync worker writing `external_sources`, `external_fields`, `external_records`, `sync_runs`, and `sync_requests`.
- `services/backend-api`: FastAPI backend for auth/RBAC/features/admin/doc-sync/webhooks.
- `services/public-web`: public homepage and business entry navigation.
- `services/admin-ui`: admin management UI.
- `deploy/ecs`: ECS Docker Compose and deploy scripts.
- `local`: local Docker Compose.

Current uncommitted partial work from this session:

- Added recipe query tests and initial `backend-api/app/recipes` code.
- Added a Feishu provider and `sync-feishu-full` worker pipeline.
- These changes still need cleanup, full tests, docs, Docker/env wiring, homepage UI, and final review before commit or push.

Do not stage unrelated dirty files:

- `deploy/openclaw-bridge/openclaw_bridge.py`
- `docs/webdock-openclaw-integration.md`
- `tests/test_openclaw_bridge.py`
- `docs/ops/`

## File Structure Target

### Backend API

- Create or keep: `services/backend-api/app/recipes/__init__.py`
  - Package marker for recipe query code.
- Create or refine: `services/backend-api/app/recipes/bom_query.py`
  - Pure workbook logic migrated from `peifangpaichan/tools/manufacturing_calc_gongju/T010_extract_source_bom_goujianbili.py` and `T012_extract_bom_detail_review.py`.
  - No FastAPI imports here.
  - Functions: locate source workbook, merge BOM parent/child sheets, query by code/name, create downloadable Excel.
- Modify: `services/backend-api/app/main.py`
  - Add authenticated `POST /v1/recipes/query`.
  - Add authenticated `GET /v1/recipes/download/{file_id}`.
  - Update `DEFAULT_FEATURES.formula_query` to active `/` homepage anchor or `/recipes/` route.
- Modify: `services/backend-api/requirements.txt`
  - Add only required workbook dependencies: `pandas`, `openpyxl`.
- Create: `services/backend-api/app/recipes/README.md`
  - Explain runtime inputs, outputs, env vars, and security boundary.

### Public Web

- Modify: `services/public-web/index.html`
  - Turn existing `formula_query` feature into an in-page recipe query panel.
  - User enters one or more recipe codes.
  - JS calls `/api/v1/recipes/query` or local `http://localhost:8000/v1/recipes/query`.
  - Show match count, recipe count, source workbook name, preview rows, and download button.
- Create: `services/public-web/README.md`
  - Explain homepage role and API dependencies.

### Doc Sync Worker

- Modify: `services/doc-sync-worker/app/providers/feishu.py`
  - Replace placeholder with env parsing, Feishu tenant token client, bitable field/record pagination.
  - Never print app secret or access token.
- Create: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`
  - Full sync Feishu bitable records into the same Postgres external data tables.
- Modify: `services/doc-sync-worker/app/main.py`
  - Add `sync-feishu-full --profiles`.
- Create: `services/doc-sync-worker/README.md`
  - Explain WeCom and Feishu sync responsibilities, commands, env vars, and limitations.

### Database and Deployment

- Modify only if needed: `db/migrations/0005_doc_sync.sql` or create a later migration.
  - Prefer reusing `external_sources`, `external_fields`, `external_records`, `sync_runs`.
  - Do not add tables unless a real query or lifecycle need appears.
- Modify: `local/docker-compose.local.yml`
  - Add Feishu env placeholders to `doc-sync-worker`.
  - Mount T+ worker output to `backend-api` read-only for recipe query.
  - Add `RECIPE_BOM_INPUT_DIR` and `RECIPE_EXPORT_DIR`.
- Modify: `deploy/ecs/compose.prod.yml`
  - Add same runtime env and volumes.
  - Keep `doc-sync-worker` as run-on-demand / scheduled worker, not backend startup.
- Modify: `deploy/ecs/runtime.env.example`
  - Add placeholders only.
- Modify: `local/.env.local.example`
  - Add placeholders only.

### Docs

- Modify: `docs/doc-sync-design.md`
  - Extend from WeCom-only to WeCom + Feishu.
- Modify: `docs/env-matrix.md`
  - Add Feishu, recipe query, and worker scheduling variables.
- Create: `docs/recipe-query.md`
  - Explain data source, query behavior, downloadable workbook sheets, and risks.
- Create: `docs/project-ai-map.md`
  - Major folder map for AI handoff.

### Tests

- Create/refine: `tests/test_recipe_query.py`
  - Workbook merge uses parent code + version.
  - Disabled BOM remains included by default.
  - Default BOM filter can select active-only.
  - Generated workbook contains human review and matrix sheets.
- Modify: `tests/test_doc_sync_worker.py`
  - Feishu env profile discovery.
  - Feishu bitable source discovery.
  - Feishu pagination using fake client responses.
- Create/refine: `tests/test_backend_recipes.py`
  - Route auth/permission behavior.
  - Query returns download URL.
  - Download rejects invalid IDs.

## Task 1: Stabilize Current Partial Changes

**Files:**

- Modify: `tests/test_recipe_query.py`
- Modify: `tests/test_doc_sync_worker.py`
- Modify: `services/backend-api/app/recipes/bom_query.py`
- Modify: `services/doc-sync-worker/app/providers/feishu.py`

- [ ] **Step 1: Check current diff and dirty files**

Run:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS status --short --branch
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS diff -- tests/test_recipe_query.py tests/test_doc_sync_worker.py services/backend-api/app/recipes/bom_query.py services/doc-sync-worker/app/providers/feishu.py
```

Expected:

- Shows only intended partial files plus known unrelated OpenClaw/docs dirt.
- Do not stage anything.

- [ ] **Step 2: Fix `app` package collision in tests**

Patch `tests/test_doc_sync_worker.py` so `FeishuProviderEnvTests.setUp` clears any `app.*` module imported from backend tests and inserts `WORKER_ROOT`.

Use this exact method:

```python
    def setUp(self) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(WORKER_ROOT))
```

Add `tearDown`:

```python
    def tearDown(self) -> None:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        worker_root = str(WORKER_ROOT)
        sys.path[:] = [item for item in sys.path if item != worker_root]
```

- [ ] **Step 3: Run focused tests**

Run:

```powershell
python -m unittest AliECS.tests.test_recipe_query AliECS.tests.test_doc_sync_worker.FeishuProviderEnvTests
```

Expected:

- `Ran 4 tests`
- `OK`

- [ ] **Step 4: Fix only failures from Step 3**

If failures are import collisions, fix test module cleanup. If failures are workbook locks, ensure `pd.ExcelFile` uses `with`. If Feishu env parsing fails, fix `profiled_env_candidates` only.

Do not touch homepage, Docker, deploy, or docs in this task.

## Task 2: Recipe Query Pure Module

**Files:**

- Test: `tests/test_recipe_query.py`
- Modify: `services/backend-api/app/recipes/bom_query.py`
- Create: `services/backend-api/app/recipes/README.md`

- [ ] **Step 1: Write/confirm failing tests for query behavior**

Tests must cover:

```python
result = query_recipe_workbook(source_path, query_text="3027", default_bom="all")
self.assertEqual(3, result.match_count)
self.assertEqual(2, result.recipe_count)
self.assertEqual({"V0", "V1"}, set(result.detail["版本号_子件"].astype(str)))
```

And:

```python
result = query_recipe_workbook(source_path, query_text="3027", default_bom="1")
self.assertEqual(2, result.match_count)
```

- [ ] **Step 2: Run test red/green check**

Run:

```powershell
python -m unittest AliECS.tests.test_recipe_query
```

Expected after implementation:

- `Ran 2 tests`
- `OK`

- [ ] **Step 3: Keep module independent**

Ensure `services/backend-api/app/recipes/bom_query.py` imports no FastAPI, no psycopg, no `.env` loader, and no source project absolute paths.

- [ ] **Step 4: Add README**

Create `services/backend-api/app/recipes/README.md`:

```markdown
# Recipe Query Module

This folder contains pure workbook logic for AliECS recipe query.

Inputs are runtime-only BOM workbooks from `services/tplus-sync-worker/output/excel` or a path configured by `RECIPE_BOM_INPUT_PATH`.
Outputs are generated review workbooks under `RECIPE_EXPORT_DIR`.

No real BOM workbook, token, `.env`, or business output belongs in Git.
```

## Task 3: Recipe Query API

**Files:**

- Create: `tests/test_backend_recipes.py`
- Modify: `services/backend-api/app/main.py`
- Modify: `services/backend-api/requirements.txt`

- [ ] **Step 1: Write failing route tests**

Create tests that import `app.main.app`, create a temp BOM workbook, set:

```python
os.environ["RECIPE_BOM_INPUT_PATH"] = str(source_path)
os.environ["RECIPE_EXPORT_DIR"] = str(tmp_path)
```

Use a signed admin token generated through `_encode_token`:

```python
payload = {"sub": "admin", "roles": ["admin"], "permissions": ["admin.access"], "exp": int(time.time()) + 3600}
token = _encode_token(payload)
```

Assert:

```python
response = client.post("/v1/recipes/query", headers={"Authorization": f"Bearer {token}"}, json={"query": "3027"})
self.assertEqual(200, response.status_code)
self.assertIn("/v1/recipes/download/", response.json()["download_url"])
```

- [ ] **Step 2: Implement minimal routes**

In `services/backend-api/app/main.py`, add:

```python
class RecipeQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    default_bom: str = "all"
    include_disabled: bool = True
```

Add helper:

```python
def require_permission(permission: str, user: dict[str, Any]) -> dict[str, Any]:
    roles = user.get("roles", [])
    permissions = user.get("permissions", [])
    if "admin" in roles or "admin.access" in permissions or permission in permissions:
        return user
    raise HTTPException(status_code=403, detail="permission denied")
```

Add routes:

```python
@app.post("/v1/recipes/query")
def recipe_query(body: RecipeQueryRequest, user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
    require_permission("formula.read", user)
    ...

@app.get("/v1/recipes/download/{file_id}")
def recipe_download(file_id: str, user: dict[str, Any] = Depends(require_login)) -> FileResponse:
    require_permission("formula.read", user)
    ...
```

- [ ] **Step 3: Update dependencies**

Append to `services/backend-api/requirements.txt`:

```text
pandas==2.2.3
openpyxl==3.1.5
```

- [ ] **Step 4: Run route tests**

Run:

```powershell
python -m unittest AliECS.tests.test_backend_recipes
```

Expected:

- `OK`

## Task 4: Homepage Recipe Query Panel

**Files:**

- Modify: `services/public-web/index.html`
- Create/modify: `services/public-web/README.md`

- [ ] **Step 1: Add a visible query section**

Add a section after `features`:

```html
<section id="recipeQueryCard" class="card">
  <h2>配方查询</h2>
  <div class="recipe-controls">
    <input id="recipeCodeInput" placeholder="输入配方编号，例如 3027；多个用空格或逗号分隔" />
    <button id="recipeQueryBtn" class="btn primary" type="button">查询配方</button>
  </div>
  <div id="recipeResult" class="notice">输入编号后生成可下载核对表。</div>
  <div id="recipePreview" class="table-wrap hidden"></div>
</section>
```

- [ ] **Step 2: Add JS API call**

Add:

```javascript
async function queryRecipe(){
  const query = recipeCodeInput.value.trim();
  if(!query){ showError('请输入配方编号。'); return; }
  const data = await api('/v1/recipes/query', {
    method:'POST',
    body: JSON.stringify({query, default_bom:'all', include_disabled:true})
  });
  recipeResult.innerHTML = `匹配明细 ${data.match_count} 行，配方 ${data.recipe_count} 个，来源：${data.source_file}。 <a class="btn primary" href="${API_BASE}${data.download_url}" target="_blank">下载核对表</a>`;
  renderRecipePreview(data.preview || []);
}
```

Add `renderRecipePreview` with columns `父件编码`, `版本号_子件`, `父件名称`, `子件编码`, `子件名称`, `需用数量`, `比例`, `停用`.

- [ ] **Step 3: Wire button**

Inside `DOMContentLoaded`:

```javascript
recipeQueryBtn.onclick = () => queryRecipe().catch(e => showError(e.message));
recipeCodeInput.onkeydown = (e) => { if(e.key === 'Enter') queryRecipe().catch(err => showError(err.message)); };
```

- [ ] **Step 4: Browser smoke**

Run local stack or static server as available. Check:

```powershell
docker compose -f local/docker-compose.local.yml config
```

Then open `http://localhost:8080` and verify:

- no overlapping text,
- query card visible,
- entering `3027` sends one API call,
- download link appears when backend returns success.

## Task 5: Feishu Full Sync Worker

**Files:**

- Modify: `services/doc-sync-worker/app/providers/feishu.py`
- Create: `services/doc-sync-worker/app/pipelines/sync_feishu_full.py`
- Modify: `services/doc-sync-worker/app/main.py`
- Modify: `tests/test_doc_sync_worker.py`

- [ ] **Step 1: Write fake-client pagination test**

Add a test for `FeishuBitableClient.get_records` using a subclass that returns two pages:

```python
pages = [
    {"code": 0, "data": {"items": [{"record_id": "r1"}], "has_more": True, "page_token": "next"}},
    {"code": 0, "data": {"items": [{"record_id": "r2"}], "has_more": False}},
]
```

Assert:

```python
self.assertEqual(["r1", "r2"], [x["record_id"] for x in result["records"]])
self.assertEqual(2, result["page_count"])
```

- [ ] **Step 2: Implement provider**

Keep these interfaces:

```python
env_profiles(profiles_arg: str = "") -> list[str]
credentials_for_profile(profile: str) -> list[FeishuCredential]
discover_profile_sources(profile: str) -> list[FeishuBitableSource]
class FeishuBitableClient
```

Never log app_secret or tenant token.

- [ ] **Step 3: Implement pipeline**

Use existing `PostgresDocSyncStore`:

```python
store.ensure_source(provider="feishu", source_type="bitable_table", external_doc_id=app_token, external_sheet_id=table_id)
store.replace_fields(source_id, fields)
store.upsert_record(source_id, build_record_snapshot(record, field_titles))
store.finish_run(...)
```

- [ ] **Step 4: Add CLI**

`services/doc-sync-worker/app/main.py`:

```python
feishu_parser = subparsers.add_parser("sync-feishu-full", help="完整同步飞书多维表格")
feishu_parser.add_argument("--profiles", default="")
```

- [ ] **Step 5: Run tests**

Run:

```powershell
python -m unittest AliECS.tests.test_doc_sync_worker
```

Expected:

- All doc-sync worker tests pass.

## Task 6: Docker and Env Wiring

**Files:**

- Modify: `local/docker-compose.local.yml`
- Modify: `deploy/ecs/compose.prod.yml`
- Modify: `local/.env.local.example`
- Modify: `deploy/ecs/runtime.env.example`
- Modify: `docs/env-matrix.md`

- [ ] **Step 1: Wire recipe volumes**

For `backend-api`, add:

```yaml
environment:
  RECIPE_BOM_INPUT_DIR: ${RECIPE_BOM_INPUT_DIR:-/app/tplus-output/excel}
  RECIPE_EXPORT_DIR: ${RECIPE_EXPORT_DIR:-/tmp/aliecs-recipe-exports}
volumes:
  - tplus_sync_output:/app/tplus-output:ro
```

For local compose, if T+ output is a bind mount, use:

```yaml
volumes:
  - ../services/tplus-sync-worker/output:/app/tplus-output:ro
```

- [ ] **Step 2: Wire Feishu worker env**

Add placeholders:

```yaml
FEISHU_ENV_PROFILES: ${FEISHU_ENV_PROFILES:-}
FEISHU_COMPANY_A_APP_ID: ${FEISHU_COMPANY_A_APP_ID:-}
FEISHU_COMPANY_A_APP_SECRET: ${FEISHU_COMPANY_A_APP_SECRET:-}
FEISHU_COMPANY_A_APP_TOKEN: ${FEISHU_COMPANY_A_APP_TOKEN:-}
FEISHU_COMPANY_A_TABLE_ID: ${FEISHU_COMPANY_A_TABLE_ID:-}
FEISHU_COMPANY_A_VIEW_ID: ${FEISHU_COMPANY_A_VIEW_ID:-}
```

Repeat for `COMPANY_B`.

- [ ] **Step 3: Validate Compose**

Run:

```powershell
docker compose -f local/docker-compose.local.yml config
```

Expected:

- Exit code 0.
- No real secrets printed.

## Task 7: Runtime Validation with Local `.env`

**Files:**

- No committed file changes.
- Use local `.env` files only for process environment or ignored `local/.env.local`.

- [ ] **Step 1: Load local env without printing values**

Use PowerShell to copy variable names and values into the current process only. Do not echo values.

```powershell
$envFiles = @(
  'C:\Users\ishel\Desktop\编程总库\peifangpaichan\.env',
  'C:\Users\ishel\Desktop\编程总库\feishu-obsidian-miner\.env'
)
foreach ($file in $envFiles) {
  if (Test-Path $file) {
    Get-Content $file | Where-Object { $_ -match '^\s*[^#][^=]+=.*$' } | ForEach-Object {
      $name, $value = $_ -split '=', 2
      [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
    }
  }
}
'loaded env names only'
```

- [ ] **Step 2: Test Feishu auth only if env exists**

Run:

```powershell
docker compose -f local/docker-compose.local.yml run --rm doc-sync-worker python -m app.main sync-feishu-full --profiles COMPANY_A
```

Expected:

- If credentials are valid and table access is allowed, one `sync_runs` row succeeds.
- If blocked by Feishu permissions, capture only error code/message, not secret/token.

- [ ] **Step 3: Test recipe query with local T+ output**

Run backend locally with:

```powershell
$env:RECIPE_BOM_INPUT_PATH='C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS\services\tplus-sync-worker\output\excel\<latest-bom>.xlsx'
python -m unittest AliECS.tests.test_backend_recipes
```

Expected:

- Query route creates a downloadable workbook.

## Task 8: AI Folder Descriptions

**Files:**

- Create/modify: `docs/project-ai-map.md`
- Create/modify: `services/backend-api/README.md`
- Create/modify: `services/backend-api/app/recipes/README.md`
- Create/modify: `services/doc-sync-worker/README.md`
- Create/modify: `services/public-web/README.md`
- Create/modify: `services/tplus-sync-worker/README.md`

- [ ] **Step 1: Add project AI map**

Create `docs/project-ai-map.md` with:

```markdown
# AliECS AI Project Map

AliECS is the server-side business platform. Do not modify `webdock` from this repo.

## services/backend-api
FastAPI API, auth/RBAC, admin APIs, webhook gateway, recipe query and downloads.

## services/doc-sync-worker
Worker-only sync for WeCom smart sheets and Feishu bitables. It calls external APIs and writes Postgres.

## services/tplus-sync-worker
Worker-only read sync for Chanjet/T+ OpenAPI. It writes raw JSON and Excel output.

## services/public-web
Public homepage and business entry UI.

## services/admin-ui
Admin UI for users, roles, features, doc-sync status and manual sync requests.

## deploy/ecs
Production compose and deployment scripts for ECS.

## local
Local Docker Compose and ignored local env file.
```

- [ ] **Step 2: Add per-folder README**

Each README must include:

- purpose,
- runtime inputs,
- runtime outputs,
- what must not be committed,
- validation commands.

## Task 9: Full Local Verification

**Files:**

- No code changes unless failures identify a root cause.

- [ ] **Step 1: Python compile**

Run:

```powershell
python -m compileall AliECS\services\backend-api\app AliECS\services\doc-sync-worker\app AliECS\services\tplus-sync-worker\src
```

Expected:

- Exit code 0.

- [ ] **Step 2: Unit tests**

Run:

```powershell
python -m unittest discover -s AliECS\tests
```

Expected:

- Exit code 0.

- [ ] **Step 3: Compose config**

Run:

```powershell
docker compose -f AliECS\local\docker-compose.local.yml config
```

Expected:

- Exit code 0.

- [ ] **Step 4: Optional local stack smoke**

Run:

```powershell
docker compose -f AliECS\local\docker-compose.local.yml up -d postgres backend-api public-web admin-ui
curl.exe -fsS http://127.0.0.1:8000/healthz
curl.exe -fsS http://127.0.0.1:8080/
```

Expected:

- backend reports `status` ok or degraded only if DB is still starting.
- public web returns HTML.

## Task 10: GitHub and ECS Sync

**Files:**

- No source edits in this task unless deployment verification exposes a concrete issue.

- [ ] **Step 1: Confirm git scope**

Run:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS status --short --branch
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS remote -v
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS branch --show-current
```

Expected:

- Branch `main`.
- Remote `git@github.com:huozao/AliECS.git`.
- Only intended AliECS paths staged later.

- [ ] **Step 2: Secret scan before staging**

Run:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS diff --name-only
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS diff -- . ':(exclude)deploy/openclaw-bridge/openclaw_bridge.py' | Select-String -Pattern 'AppSecret|openToken|tenant_access_token|CHANJET_OPEN_TOKEN|FEISHU_.*SECRET|WECOM_.*SECRET|[A-Za-z0-9+/=]{40,}' -CaseSensitive
```

Expected:

- No matches in committed diff.

- [ ] **Step 3: Stage explicit files only**

Stage only implementation files from this plan. Do not use broad `git add .`.

Example:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS add services/backend-api/app/recipes services/backend-api/app/main.py services/backend-api/requirements.txt tests/test_recipe_query.py tests/test_backend_recipes.py services/doc-sync-worker/app/providers/feishu.py services/doc-sync-worker/app/pipelines/sync_feishu_full.py services/doc-sync-worker/app/main.py tests/test_doc_sync_worker.py local/docker-compose.local.yml deploy/ecs/compose.prod.yml local/.env.local.example deploy/ecs/runtime.env.example docs/doc-sync-design.md docs/env-matrix.md docs/recipe-query.md docs/project-ai-map.md services/doc-sync-worker/README.md services/public-web/README.md
```

- [ ] **Step 4: Commit and push**

Only after verification passes:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS commit -m "feat: add Feishu sync and recipe query"
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS push origin main
```

- [ ] **Step 5: Confirm GitHub Actions**

Run:

```powershell
& 'C:\Program Files\GitHub CLI\gh.exe' run list --repo huozao/AliECS --workflow release-deploy.yml --limit 3
```

Expected:

- New run appears for pushed commit.

- [ ] **Step 6: Verify ECS**

Use SSH alias `aliecs`:

```powershell
ssh aliecs "cd /root/AliECS && git log -1 --oneline && docker compose -f deploy/ecs/compose.prod.yml ps && curl -fsS http://127.0.0.1:8000/healthz"
```

Expected:

- ECS commit matches GitHub.
- backend health responds.
- No `webdock` repo touched.

## Execution Order

Recommended order:

1. Task 1
2. Task 2
3. Task 3
4. Task 5
5. Task 6
6. Task 4
7. Task 8
8. Task 9
9. Task 7, only when local credentials are needed
10. Task 10, only after user confirms commit/push/deploy

Parallel-safe groups:

- Task 2 and Task 5 can be implemented independently after Task 1.
- Task 4 can start after Task 3 API contract is stable.
- Task 8 can run after file/folder boundaries are final.

Must be serial:

- Any command that writes `.git`: `git add`, `git commit`, `git push`.
- Any production deployment verification on ECS.
- Any operation using local real `.env` values.

## Verification Checklist

- `python -m compileall ...` passes.
- `python -m unittest discover -s AliECS\tests` passes.
- `docker compose -f AliECS\local\docker-compose.local.yml config` passes.
- Homepage recipe query shows preview and download link.
- Downloaded workbook opens and contains `配方表_人眼版`, `横向对比_矩阵`, `父件子件明细_提取`, `版本父件分组合计`.
- Feishu sync writes provider `feishu` rows to `external_sources`, `external_fields`, `external_records`, `sync_runs`.
- No real `.env`, AppSecret, openToken, tenant token, app ticket, raw business data, output files, logs, cache, `.venv`, or `__pycache__` are staged.

## Remaining Risks

- T+ BOM export coverage depends on Chanjet API returning full active and disabled BOM versions. If OpenAPI omits disabled records, recipe query can only reflect what the worker exports.
- Feishu and WeCom API calls may require trusted IP, app permissions, table permissions, or tenant approval. These are runtime/account issues, not code issues.
- pandas/openpyxl in backend increases image size; acceptable for first version because recipe downloads are user-facing and workbook-heavy.
- Existing `backend-api/app/main.py` is large. This plan keeps changes minimal, but future work should split routers after this feature is stable.
- Current workspace has unrelated dirty OpenClaw files; commit scope must stay explicit.
