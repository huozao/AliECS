# Couple Immich Long-Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Couple Memory with an independently maintained Immich deployment so Codex can run the full rollout for a long time, verify each phase, and stop only at explicit safety boundaries.

**Architecture:** Immich runs as its own official Docker Compose service on the old laptop or NAS and owns original media, thumbnails, metadata, search, mobile backup, and upgrades. AliECS owns Couple Memory business data and stores only references to Immich assets through a small backend adapter and binding table. The rollout is phase-based, resumable, and records progress in `docs/ops/couple-immich-handoff.md`.

**Tech Stack:** AliECS FastAPI backend, Postgres migrations, static public-web pages, Docker Compose, Nginx reverse proxy, Immich official Docker Compose, PowerShell locally, SSH aliases `aliecs` and `webdock`.

---

## Operating Rules

- Execute phases in order.
- Do not ask for user input unless a hard stop condition is hit.
- After each phase, update `docs/ops/couple-immich-handoff.md` with completed work, commands run, verification result, current blockers, and next recommended step.
- Do not commit or push unless the user explicitly asks.
- Do not modify `.env` files in git. Only update `.env.example`, docs, scripts, tests, and code.
- Never stage or commit `.env`, logs, browser data, original photos, Immich database files, generated backups, or `_references`.
- If a command fails, identify the failing boundary first. Continue with independent safe tasks when possible.

## Hard Stop Conditions

Stop and ask the user when any of these occur:

- The next command could overwrite, delete, migrate, restore, or replace existing Immich media or database files.
- Backup preflight fails and the next step is Immich upgrade, database restore, storage migration, or destructive cleanup.
- `ssh aliecs` or `ssh webdock` fails repeatedly after one retry.
- Immich first-admin setup, API key creation, or mobile app login needs human credentials.
- Nginx or DNS changes would alter an existing production route other than the new `immich.hydwang.xyz` route.
- Git staging includes `.env`, logs, browser data, original photos, generated backups, or `_references`.

## Resume Protocol

When resuming after interruption:

1. Read this plan.
2. Read `docs/superpowers/specs/2026-06-12-couple-immich-long-run-design.md`.
3. Read `docs/ops/couple-immich-handoff.md` if it exists.
4. Run `git status --short` in `AliECS` and `webdock`.
5. Continue from the first incomplete phase that is safe to run.

## Current Execution State

Last reviewed: 2026-06-12.

- Phase 0 through Phase 7 have been executed once. See `docs/ops/couple-immich-handoff.md` for command evidence and exact results.
- Do not rerun Phase 1 service bootstrap as a blind replay: Immich already exists at `webdock:~/immich-app` and contains runtime `.env`, `library`, and `postgres` directories.
- Do not rerun tunnel or Nginx edits as a blind replay: ECS currently has a working `127.0.0.1:12283` reverse tunnel and `/etc/nginx/conf.d/immich.conf` Host-header proxy for `immich.hydwang.xyz`.
- Phase 6 maintenance scripts exist in the `webdock` repo and have been copied to `webdock:~/immich-app/`.
- Safe update is intentionally blocked until `backup-immich-preflight.sh` finds a recent real database backup under `~/immich-app/library/backups`.
- Remaining work is external/manual: DNS for `immich.hydwang.xyz`, TLS certificate coverage, Immich first-admin setup, Immich API key creation, and runtime-only `IMMICH_API_KEY` configuration.
- If resuming after those external tasks are done, start with Phase 7 remote health checks and then enable AliECS runtime Immich integration.

## Operator Prompt Template

Paste this into a fresh Codex thread from `C:\Users\ishel\Desktop\编程总库\AliECS-WebDock`:

```text
[@superpowers](plugin://superpowers@openai-curated)
请按 AliECS/docs/superpowers/plans/2026-06-12-couple-immich-long-run.md 长时间连续执行 Couple + Immich 集成。

工作规则：
1. 默认不要停下来问我，按计划一阶段一阶段执行、验证、记录。
2. 每个阶段完成后更新 AliECS/docs/ops/couple-immich-handoff.md。
3. 只有计划里的 Hard Stop Conditions 触发时才停下来问我。
4. 不要提交或推送，除非我另行明确要求。
5. 不要泄露、提交或输出 .env、API key、照片原图、数据库备份、logs、browser_data、_references。
6. 若真实 Immich API key 或首次管理员登录缺失，先完成 mock/fake-client 可验证的 AliECS 代码、测试、文档和部署脚本，然后把人工待办写入 handoff。
7. 遇到失败先用 systematic-debugging 定位边界；能安全继续的独立任务继续做。
8. 最后使用 verification-before-completion 做全量验证并汇总证据。
```

## File Map

AliECS files likely to change:

- `docs/superpowers/specs/2026-06-12-couple-immich-long-run-design.md`: design source of truth.
- `docs/superpowers/plans/2026-06-12-couple-immich-long-run.md`: this execution plan.
- `docs/ops/couple-immich-runbook.md`: operator runbook for Immich deployment, backup, update, and restore boundaries.
- `docs/ops/couple-immich-handoff.md`: append/update progress after each phase.
- `docs/env-matrix.md`: Immich env variables and failure modes.
- `deploy/ecs/runtime.env.example`: example-only Immich runtime variables.
- `deploy/ecs/release-meta.env.example`: example-only Immich release variables.
- `deploy/ecs/compose.prod.yml`: pass Immich env variables to backend-api only if needed.
- `db/migrations/0004_couple_immich_assets.sql`: binding table.
- `services/backend-api/app/main.py`: narrow routes if keeping current single-file pattern.
- `services/backend-api/app/immich_client.py`: preferred new adapter module if local import pattern allows.
- `services/public-web/couple/index.html`: gallery and status integration.
- `services/public-web/memories/detail.html`: bind and display Immich assets.
- `services/public-web/s/index.html`: share page display for bound assets.
- `tests/test_couple_immich_client.py`: adapter tests.
- `tests/test_couple_immich_assets.py`: binding route tests.

webdock files likely to change:

- `docs/operations.md`: Immich host operation notes if webdock remains old-laptop control repo.
- `deploy/laptop/immich/README.md`: local deployment notes if repo-managed scripts are desired.
- `deploy/laptop/immich/check-immich-health.sh`: safe health check.
- `deploy/laptop/immich/backup-immich-preflight.sh`: backup preflight.
- `deploy/laptop/immich/update-immich-safe.sh`: guarded update script.

## Phase 0: Read-Only Audit

**Goal:** Establish current state without modifying services.

**Files:**

- Modify: `AliECS/docs/ops/couple-immich-handoff.md`

- [ ] **Step 0.1: Inspect git state**

Run:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS status --short
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\webdock status --short
```

Expected:

- Dirty state is allowed.
- Record unrelated dirty files in handoff.
- Do not revert unrelated changes.

- [ ] **Step 0.2: Check SSH reachability**

Run:

```powershell
ssh aliecs "hostname; date; docker --version || true"
ssh webdock "hostname; date; docker --version || true"
```

Expected:

- Both aliases respond.
- If one alias fails, retry once.
- If it fails twice, stop under hard stop condition.

- [ ] **Step 0.3: Read-only old laptop storage audit**

Run:

```powershell
ssh webdock "set -eu; df -h; docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true; test -d ~/immich-app && ls -la ~/immich-app || true; test -d /opt/immich && ls -la /opt/immich || true"
```

Expected:

- Disk space and existing Immich paths are known.
- Existing Immich data triggers caution but not automatic overwrite.

- [ ] **Step 0.4: Create or update handoff**

Before editing, state that only `AliECS/docs/ops/couple-immich-handoff.md` will be changed.

Add:

```markdown
# Couple Immich Handoff

## Current Phase

Phase 0 read-only audit.

## Completed Work

- Recorded AliECS and webdock git status.
- Checked SSH reachability for `aliecs` and `webdock`.
- Checked Docker and disk state on old laptop.

## Verification

- Commands run:
  - `git -C ...\AliECS status --short`
  - `git -C ...\webdock status --short`
  - `ssh aliecs "hostname; date; docker --version || true"`
  - `ssh webdock "hostname; date; docker --version || true"`
  - `ssh webdock "set -eu; df -h; docker ps ..."`

## Blockers

- List only actual blockers observed in this run.

## Next Step

Continue to Phase 1 when no overwrite risk exists for existing Immich data.
```

- [ ] **Step 0.5: Verify handoff exists**

Run:

```powershell
Test-Path C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS\docs\ops\couple-immich-handoff.md
```

Expected: `True`.

## Phase 1: Immich Base Service

**Goal:** Deploy Immich as an independent service on the old laptop without copying secrets into git.

**Files:**

- Modify: `AliECS/docs/ops/couple-immich-runbook.md`
- Modify: `AliECS/docs/ops/couple-immich-handoff.md`
- Optionally create in webdock repo: `deploy/laptop/immich/README.md`

- [ ] **Step 1.1: Detect existing Immich path**

Run:

```powershell
ssh webdock "set -eu; for d in ~/immich-app /opt/immich /srv/immich; do if [ -e \"$d\" ]; then echo FOUND:$d; ls -la \"$d\"; fi; done"
```

Expected:

- If existing compose/data paths are found, stop before overwrite.
- If no existing path, continue.

- [ ] **Step 1.2: Prepare official compose directory**

Run only if no overwrite risk exists:

```powershell
ssh webdock "set -eu; mkdir -p ~/immich-app; cd ~/immich-app; test -f docker-compose.yml || wget -O docker-compose.yml https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml; test -f .env || wget -O .env https://github.com/immich-app/immich/releases/latest/download/example.env; chmod 600 .env"
```

Expected:

- Compose file and remote `.env` exist on old laptop.
- No secret is printed.

- [ ] **Step 1.3: Configure remote `.env` safely**

Run:

```powershell
ssh webdock "set -eu; cd ~/immich-app; cp .env .env.pre-codex.$(date +%Y%m%d%H%M%S); sed -i 's#^UPLOAD_LOCATION=.*#UPLOAD_LOCATION=./library#' .env; sed -i 's#^DB_DATA_LOCATION=.*#DB_DATA_LOCATION=./postgres#' .env; grep -q '^TZ=' .env && sed -i 's#^TZ=.*#TZ=Asia/Shanghai#' .env || printf '\nTZ=Asia/Shanghai\n' >> .env; grep -q '^IMMICH_VERSION=' .env && sed -i 's#^IMMICH_VERSION=.*#IMMICH_VERSION=v2#' .env || printf '\nIMMICH_VERSION=v2\n' >> .env; if grep -q '^DB_PASSWORD=postgres$' .env; then python3 - <<'PY'\nfrom pathlib import Path\nimport secrets,string\np=Path('.env')\nchars=string.ascii_letters+string.digits\npwd=''.join(secrets.choice(chars) for _ in range(32))\ns=p.read_text()\ns=s.replace('DB_PASSWORD=postgres', 'DB_PASSWORD='+pwd)\np.write_text(s)\nPY\nfi; grep -E '^(UPLOAD_LOCATION|DB_DATA_LOCATION|TZ|IMMICH_VERSION)=' .env"
```

Expected:

- Prints non-secret path/version values.
- Does not print `DB_PASSWORD`.

- [ ] **Step 1.4: Start Immich**

Run:

```powershell
ssh webdock "set -eu; cd ~/immich-app; docker compose up -d; docker compose ps"
```

Expected:

- Immich containers start.
- If Docker is missing or compose syntax fails, record boundary and continue only with AliECS mock work.

- [ ] **Step 1.5: Verify local Immich ping**

Run:

```powershell
ssh webdock "set -eu; for i in $(seq 1 30); do if curl -fsS http://127.0.0.1:2283/api/server/ping; then exit 0; fi; sleep 2; done; docker compose -f ~/immich-app/docker-compose.yml logs --tail=120 immich-server; exit 1"
```

Expected:

- Ping succeeds.
- On failure, capture key logs in handoff and continue with code/docs tasks that do not require live Immich.

- [ ] **Step 1.6: Document runbook**

Add to `AliECS/docs/ops/couple-immich-runbook.md`:

```markdown
# Couple Immich Runbook

## Service Location

- Host: `webdock`
- Directory: `~/immich-app`
- Public route: `immich.hydwang.xyz` after Phase 2
- Local health: `http://127.0.0.1:2283/api/server/ping`

## Data Locations

- Uploads: `~/immich-app/library`
- Database data: `~/immich-app/postgres`
- Runtime env: `~/immich-app/.env`

## Safety Rules

- Do not delete or replace `library` or `postgres` without a verified backup and user approval.
- Do not print `DB_PASSWORD` or API keys.
- Do not commit remote `.env` content.
```

- [ ] **Step 1.7: Update handoff**

Record:

- Immich path.
- Compose status.
- Local ping result.
- Any manual action required, especially first-admin setup.

## Phase 2: Public Access

**Goal:** Expose Immich through a dedicated root subdomain without affecting existing Couple routes.

**Files:**

- Modify: `AliECS/docs/ops/couple-immich-runbook.md`
- Modify: `AliECS/docs/ops/couple-immich-handoff.md`
- Modify ECS Nginx config only after inspecting current config path.

- [ ] **Step 2.1: Inspect ECS Nginx layout**

Run:

```powershell
ssh aliecs "set -eu; nginx -T 2>/tmp/nginx-all.txt || true; grep -R \"server_name .*hydwang\" -n /etc/nginx /etc/nginx/conf.d /etc/nginx/sites-enabled 2>/dev/null || true"
```

Expected:

- Current config path is known.
- If config is managed by deploy scripts, inspect repo deploy docs before editing remote files.

- [ ] **Step 2.2: Verify private path from ECS to old laptop Immich**

Run:

```powershell
ssh aliecs "set -eu; curl -fsS http://127.0.0.1:2283/api/server/ping || curl -fsS http://host.docker.internal:2283/api/server/ping || true"
```

Expected:

- At least one candidate route works, or failure boundary is recorded.
- If no route works, configure or repair tunnel only if existing project docs identify the correct path.

- [ ] **Step 2.3: Add Nginx reverse proxy only for new subdomain**

Use this shape, adapting only backend URL and certificate paths discovered on ECS:

```nginx
server {
    server_name immich.hydwang.xyz;

    client_max_body_size 50000M;
    proxy_request_buffering off;
    proxy_buffering off;

    location / {
        proxy_pass http://127.0.0.1:2283;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Expected:

- No edits to existing `hydwang.xyz/couple/` location blocks.
- If certificate issuance requires interactive DNS/account action, stop and record.

- [ ] **Step 2.4: Test and reload Nginx**

Run:

```powershell
ssh aliecs "set -eu; sudo nginx -t; sudo systemctl reload nginx; curl -fsS https://immich.hydwang.xyz/api/server/ping"
```

Expected:

- Nginx syntax passes.
- Public ping succeeds.
- If TLS is not ready, record exact failure and continue with AliECS code tasks.

- [ ] **Step 2.5: Update runbook and handoff**

Record:

- Config path changed.
- Backend URL.
- Public ping result.
- TLS or DNS blockers.

## Phase 3: AliECS Immich Adapter

**Goal:** Add a narrow, mock-testable Immich integration boundary to AliECS.

**Files:**

- Modify: `AliECS/docs/env-matrix.md`
- Modify: `AliECS/deploy/ecs/runtime.env.example`
- Modify: `AliECS/deploy/ecs/release-meta.env.example`
- Modify: `AliECS/deploy/ecs/compose.prod.yml`
- Create: `AliECS/services/backend-api/app/immich_client.py`
- Modify: `AliECS/services/backend-api/app/main.py`
- Create: `AliECS/tests/test_couple_immich_client.py`
- Modify: `AliECS/docs/ops/couple-immich-handoff.md`

- [ ] **Step 3.1: Write failing adapter tests**

Create `AliECS/tests/test_couple_immich_client.py`:

```python
import json
import unittest
from unittest.mock import Mock, patch

from services.backend_api.app.immich_client import ImmichAsset, ImmichClient, ImmichConfig


class ImmichClientTests(unittest.TestCase):
    def test_disabled_client_reports_disabled(self):
        client = ImmichClient(ImmichConfig(enabled=False, base_url="", api_key="", timeout_seconds=5))
        self.assertEqual(client.status()["enabled"], False)
        self.assertEqual(client.status()["ok"], False)

    @patch("services.backend_api.app.immich_client.urllib.request.urlopen")
    def test_ping_uses_api_key_and_normalizes_success(self, urlopen):
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b'{"res":"pong"}'
        urlopen.return_value = response

        client = ImmichClient(ImmichConfig(enabled=True, base_url="https://immich.example", api_key="secret", timeout_seconds=5))

        self.assertEqual(client.ping(), True)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://immich.example/api/server/ping")
        self.assertEqual(request.headers["X-api-key"], "secret")

    @patch("services.backend_api.app.immich_client.urllib.request.urlopen")
    def test_get_asset_normalizes_response(self, urlopen):
        payload = {
            "id": "asset-1",
            "originalFileName": "a.jpg",
            "fileCreatedAt": "2026-03-20T12:00:00Z",
            "exifInfo": {"latitude": 30.1, "longitude": 120.2},
        }
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = json.dumps(payload).encode("utf-8")
        urlopen.return_value = response

        client = ImmichClient(ImmichConfig(enabled=True, base_url="https://immich.example/", api_key="secret", timeout_seconds=5))
        asset = client.get_asset("asset-1")

        self.assertIsInstance(asset, ImmichAsset)
        self.assertEqual(asset.asset_id, "asset-1")
        self.assertEqual(asset.original_filename, "a.jpg")
        self.assertEqual(asset.latitude, 30.1)
        self.assertEqual(asset.longitude, 120.2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3.2: Verify tests fail**

Run:

```powershell
python -m unittest AliECS.tests.test_couple_immich_client -v
```

Expected:

- Fails because `immich_client.py` does not exist.

- [ ] **Step 3.3: Implement adapter**

Create `AliECS/services/backend-api/app/immich_client.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import urllib.error
import urllib.parse
import urllib.request


@dataclass(frozen=True)
class ImmichConfig:
    enabled: bool
    base_url: str
    api_key: str
    timeout_seconds: int = 20


@dataclass(frozen=True)
class ImmichAsset:
    asset_id: str
    original_filename: str | None
    taken_at: str | None
    latitude: float | None
    longitude: float | None


def load_immich_config() -> ImmichConfig:
    enabled = os.getenv("IMMICH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    timeout_raw = os.getenv("IMMICH_TIMEOUT_SECONDS", "20").strip()
    try:
        timeout = int(timeout_raw)
    except ValueError:
        timeout = 20
    return ImmichConfig(
        enabled=enabled,
        base_url=os.getenv("IMMICH_BASE_URL", "").strip().rstrip("/"),
        api_key=os.getenv("IMMICH_API_KEY", "").strip(),
        timeout_seconds=max(1, timeout),
    )


class ImmichClient:
    def __init__(self, config: ImmichConfig | None = None):
        self.config = config or load_immich_config()

    def status(self) -> dict:
        if not self.config.enabled:
            return {"enabled": False, "ok": False, "detail": "Immich integration disabled"}
        if not self.config.base_url:
            return {"enabled": True, "ok": False, "detail": "IMMICH_BASE_URL is required"}
        if not self.config.api_key:
            return {"enabled": True, "ok": False, "detail": "IMMICH_API_KEY is required"}
        return {"enabled": True, "ok": self.ping(), "detail": "ok"}

    def ping(self) -> bool:
        if not self.config.enabled or not self.config.base_url or not self.config.api_key:
            return False
        try:
            self._request_json("/api/server/ping")
            return True
        except (urllib.error.URLError, TimeoutError, ValueError):
            return False

    def get_asset(self, asset_id: str) -> ImmichAsset:
        payload = self._request_json(f"/api/assets/{urllib.parse.quote(asset_id, safe='')}")
        exif = payload.get("exifInfo") or {}
        return ImmichAsset(
            asset_id=str(payload.get("id") or asset_id),
            original_filename=payload.get("originalFileName"),
            taken_at=payload.get("fileCreatedAt") or payload.get("localDateTime"),
            latitude=exif.get("latitude"),
            longitude=exif.get("longitude"),
        )

    def _request_json(self, path: str) -> dict:
        if not self.config.enabled:
            raise ValueError("Immich integration disabled")
        if not self.config.base_url:
            raise ValueError("IMMICH_BASE_URL is required")
        if not self.config.api_key:
            raise ValueError("IMMICH_API_KEY is required")
        request = urllib.request.Request(
            f"{self.config.base_url}{path}",
            headers={"x-api-key": self.config.api_key, "accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
            raw = response.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))
```

- [ ] **Step 3.4: Add env examples**

Append example-only values to both env example files:

```env
IMMICH_ENABLED=false
IMMICH_BASE_URL=https://immich.hydwang.xyz
IMMICH_API_KEY=
IMMICH_TIMEOUT_SECONDS=20
IMMICH_PROXY_MODE=backend
```

Update `docs/env-matrix.md` with:

```markdown
| `IMMICH_ENABLED` | backend-api | Enables Couple -> Immich API integration. Default `false`. | If false, Couple uses existing local/WebDock photo behavior only. |
| `IMMICH_BASE_URL` | backend-api | Immich public or private base URL, no trailing slash. | Required when `IMMICH_ENABLED=true`. |
| `IMMICH_API_KEY` | backend-api secret | Immich API key. Keep only in runtime `.env` or secret store. | Required when `IMMICH_ENABLED=true`; never commit a real value. |
| `IMMICH_TIMEOUT_SECONDS` | backend-api | HTTP timeout for Immich API calls. Default `20`. | Slow Immich calls fail predictably. |
| `IMMICH_PROXY_MODE` | backend-api | `backend` means Couple pages fetch thumbnails through AliECS. | Prevents exposing Immich API key to browser. |
```

- [ ] **Step 3.5: Add status route**

In `services/backend-api/app/main.py`, add a route near existing Couple health/access routes:

```python
@app.get("/v1/immich/status")
def immich_status(user: dict = Depends(require_user)):
    require_permission(user, "couple_memory_access")
    from .immich_client import ImmichClient

    return ImmichClient().status()
```

If local import style rejects relative import, use the repo's established import pattern.

- [ ] **Step 3.6: Run adapter tests**

Run:

```powershell
python -m unittest AliECS.tests.test_couple_immich_client -v
python -m unittest discover -s tests -v
```

Expected:

- New adapter tests pass.
- Existing test suite passes or unrelated failures are documented.

- [ ] **Step 3.7: Update handoff**

Record:

- Env keys added.
- Tests run.
- Real Immich status if available.

## Phase 4: Couple Binding Model

**Goal:** Bind Immich assets to Couple memories with Couple permission checks.

**Files:**

- Create: `AliECS/db/migrations/0004_couple_immich_assets.sql`
- Modify: `AliECS/services/backend-api/app/main.py`
- Create: `AliECS/tests/test_couple_immich_assets.py`
- Modify: `AliECS/docs/ops/couple-immich-handoff.md`

- [ ] **Step 4.1: Create migration**

Create `db/migrations/0004_couple_immich_assets.sql` with the SQL from the spec's Data Model section.

- [ ] **Step 4.2: Write route tests first**

Create tests that cover:

- Authenticated Couple user can bind an Immich asset to a memory in their space.
- User cannot bind asset to a memory outside their Couple Space.
- Listing a memory returns bound Immich assets in `sort_order`.
- Deleting a binding removes only that binding.

Use the existing Couple tests as local style reference and fake the Immich client.

- [ ] **Step 4.3: Implement routes**

Add routes:

```text
POST /v1/memories/{memory_id}/immich-assets
GET /v1/memories/{memory_id}/immich-assets
DELETE /v1/memories/{memory_id}/immich-assets/{binding_id}
```

Route behavior:

- Always call the existing Couple access helper before reading/writing.
- Verify memory belongs to the user's active Couple Space.
- If `IMMICH_ENABLED=false`, allow manual binding only when request includes `original_filename`; otherwise return 503 with a clear message.
- If `IMMICH_ENABLED=true`, normalize asset metadata through `ImmichClient.get_asset(asset_id)`.

- [ ] **Step 4.4: Run database and route tests**

Run:

```powershell
python -m unittest AliECS.tests.test_couple_immich_assets -v
python -m unittest discover -s tests -v
```

Expected:

- New tests pass.
- Existing Couple tests pass.

- [ ] **Step 4.5: Update handoff**

Record:

- Migration file.
- Routes added.
- Tests run.
- Any live database migration not yet applied.

## Phase 5: Couple UI Integration

**Goal:** Make Immich-bound assets visible and manageable from Couple pages.

**Files:**

- Modify: `AliECS/services/public-web/memories/detail.html`
- Modify: `AliECS/services/public-web/couple/index.html`
- Modify: `AliECS/services/public-web/s/index.html`
- Modify: `AliECS/tests` only if the repo has frontend/static smoke tests.
- Modify: `AliECS/docs/ops/couple-immich-handoff.md`

- [ ] **Step 5.1: Inspect current UI API helpers**

Run:

```powershell
rg -n "function api|const API_BASE|photos|share" services/public-web/memories/detail.html services/public-web/couple/index.html services/public-web/s/index.html
```

Expected:

- Identify existing fetch helpers and rendering style.

- [ ] **Step 5.2: Add Memory detail bound asset section**

In `memories/detail.html`:

- Add a section titled `Immich 照片`.
- On load, call `/v1/memories/{id}/immich-assets`.
- Render thumbnails through `/api/v1/immich/assets/{asset_id}/thumbnail` if implemented, or show filename/taken date until proxy exists.
- Add a compact form for `immich_asset_id` manual binding.

- [ ] **Step 5.3: Add Couple dashboard status**

In `couple/index.html`:

- Call `/v1/immich/status` during dashboard load.
- Show a small status line:
  - Disabled: `Immich 未启用，当前使用本地/旧电脑照片通道。`
  - Enabled ok: `Immich 已连接。`
  - Enabled failed: `Immich 暂不可用，已保留回忆数据。`

- [ ] **Step 5.4: Add share page support**

In `s/index.html`:

- Render bound Immich assets only when returned by share API.
- Never link to the full Immich library from public share pages.

- [ ] **Step 5.5: Run static checks**

Run:

```powershell
python -m unittest discover -s tests -v
```

If a local server exists or can be safely started:

```powershell
docker compose -f deploy/ecs/compose.prod.yml config
```

Expected:

- Tests pass.
- Compose config is valid.

- [ ] **Step 5.6: Browser smoke**

If a local or deployed target is available, open:

- `/couple/`
- `/memories/`
- `/memories/detail.html?id=<known-memory-id>`
- `/s/<known-share-token>`

Verify:

- Existing content still renders.
- Immich disabled state is graceful.
- No browser console failure blocks the page.

- [ ] **Step 5.7: Update handoff**

Record UI changes, browser target, and verification result.

## Phase 6: Maintenance Automation

**Goal:** Make Immich updates repeatable and guarded by backup checks.

**Files:**

- Create: `webdock/deploy/laptop/immich/check-immich-health.sh`
- Create: `webdock/deploy/laptop/immich/backup-immich-preflight.sh`
- Create: `webdock/deploy/laptop/immich/update-immich-safe.sh`
- Modify: `webdock/docs/operations.md`
- Modify: `AliECS/docs/ops/couple-immich-runbook.md`
- Modify: `AliECS/docs/ops/couple-immich-handoff.md`

- [ ] **Step 6.1: Create health script**

Before editing, state intended changes in webdock files.

Create `webdock/deploy/laptop/immich/check-immich-health.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

IMMICH_DIR="${IMMICH_DIR:-$HOME/immich-app}"
cd "$IMMICH_DIR"

docker compose ps
curl -fsS "http://127.0.0.1:2283/api/server/ping" >/dev/null
echo "immich-health-ok"
```

- [ ] **Step 6.2: Create backup preflight script**

Create `webdock/deploy/laptop/immich/backup-immich-preflight.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

IMMICH_DIR="${IMMICH_DIR:-$HOME/immich-app}"
cd "$IMMICH_DIR"

if [ ! -f .env ]; then
  echo "missing .env in $IMMICH_DIR" >&2
  exit 1
fi

set -a
. ./.env
set +a

UPLOAD_LOCATION="${UPLOAD_LOCATION:-./library}"
DB_DATA_LOCATION="${DB_DATA_LOCATION:-./postgres}"

test -d "$UPLOAD_LOCATION"
test -d "$DB_DATA_LOCATION"
test -w "$UPLOAD_LOCATION"
test -w "$DB_DATA_LOCATION"

if ! find "$UPLOAD_LOCATION/backups" -type f -mtime -2 2>/dev/null | grep -q .; then
  echo "no recent Immich database backup found under $UPLOAD_LOCATION/backups" >&2
  exit 1
fi

echo "immich-backup-preflight-ok"
```

- [ ] **Step 6.3: Create safe update script**

Create `webdock/deploy/laptop/immich/update-immich-safe.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

IMMICH_DIR="${IMMICH_DIR:-$HOME/immich-app}"
cd "$IMMICH_DIR"

"$(dirname "$0")/backup-immich-preflight.sh"

docker compose pull
docker compose up -d
"$(dirname "$0")/check-immich-health.sh"

echo "immich-update-ok"
```

- [ ] **Step 6.4: Make scripts executable**

Run:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\webdock update-index --chmod=+x deploy/laptop/immich/check-immich-health.sh deploy/laptop/immich/backup-immich-preflight.sh deploy/laptop/immich/update-immich-safe.sh
```

Expected:

- Git mode change is recorded if files are tracked.
- Do not commit unless user asks.

- [ ] **Step 6.5: Document maintenance**

Add to `webdock/docs/operations.md` and `AliECS/docs/ops/couple-immich-runbook.md`:

```markdown
## Immich Maintenance

Health:

```bash
IMMICH_DIR=~/immich-app deploy/laptop/immich/check-immich-health.sh
```

Backup preflight:

```bash
IMMICH_DIR=~/immich-app deploy/laptop/immich/backup-immich-preflight.sh
```

Safe update:

```bash
IMMICH_DIR=~/immich-app deploy/laptop/immich/update-immich-safe.sh
```

The update script refuses to continue unless a recent database backup exists. Media files still require a separate backup of `UPLOAD_LOCATION`.
```

- [ ] **Step 6.6: Verify scripts syntax**

Run:

```powershell
ssh webdock "bash -n ~/immich-app/check-immich-health.sh"
```

If scripts are not copied to remote host by this plan, run local syntax checks with Git Bash or WSL if available, otherwise record that remote syntax verification remains.

- [ ] **Step 6.7: Update handoff**

Record scripts created, syntax checks, and whether remote installation was performed.

## Phase 7: Full Verification

**Goal:** Prove the whole rollout is safe, tested, and resumable.

**Files:**

- Modify: `AliECS/docs/ops/couple-immich-handoff.md`

- [ ] **Step 7.1: AliECS tests**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: all relevant tests pass. If unrelated tests fail, record exact failing tests and key error lines.

- [ ] **Step 7.2: Compose validation**

Run:

```powershell
docker compose -f deploy/ecs/compose.prod.yml config
```

Expected: compose config renders successfully.

- [ ] **Step 7.3: Remote health**

Run when SSH is available:

```powershell
ssh webdock "cd ~/immich-app && docker compose ps && curl -fsS http://127.0.0.1:2283/api/server/ping"
ssh aliecs "curl -fsS https://immich.hydwang.xyz/api/server/ping || true"
```

Expected:

- Local Immich health succeeds.
- Public health succeeds or exact DNS/TLS/tunnel blocker is recorded.

- [ ] **Step 7.4: Secret and media staging check**

Run:

```powershell
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS status --short
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\webdock status --short
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\AliECS status --short | Select-String -Pattern "\.env|logs|browser_data|_references|backups|library|postgres"
git -C C:\Users\ishel\Desktop\编程总库\AliECS-WebDock\webdock status --short | Select-String -Pattern "\.env|logs|browser_data|_references|backups|library|postgres"
```

Expected:

- No secrets, logs, browser data, backups, Immich library, or database paths appear in staged or unstaged git output.
- If they appear, stop and report.

- [ ] **Step 7.5: Final handoff**

Update `docs/ops/couple-immich-handoff.md` with:

```markdown
## Final Verification

- AliECS tests:
- Compose validation:
- webdock Immich health:
- ECS public Immich health:
- Couple browser smoke:
- Secret/media git check:

## Remaining Manual Actions

- Create Immich first admin if not completed.
- Create Immich API key if not completed.
- Configure mobile app backup on phones.
- Confirm media backup destination for `UPLOAD_LOCATION`.

## Next Recommended Step

Run a small end-to-end acceptance test:
1. Upload one phone photo to Immich.
2. Create one Couple Memory.
3. Bind the uploaded Immich asset.
4. Open the memory detail page.
5. Create and open a share link.
```

## Completion Criteria

The long run is complete when:

- Immich is installed or the blocker is documented.
- AliECS can run without real Immich credentials using fake/mock tests.
- Couple Memory can bind and display Immich asset references.
- Existing local/WebDock photo behavior is not removed.
- Maintenance scripts exist and refuse unsafe updates when backup preflight fails.
- Final handoff contains verification evidence and remaining manual actions.
