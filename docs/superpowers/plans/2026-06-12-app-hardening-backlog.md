# App Hardening Backlog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the remaining P0/P1/P2 hardening items from `docs/next-step-improvement-plan.md` that are still pure-code work: structured logging, upload-disk observability, session revocation, deploy-script safety checks, a real OSS photo storage driver, and CI migration/smoke coverage.

**Architecture:** All changes land in the existing `AliECS` repo on top of `main` (`937bee7`). No new services, no new runtime dependencies (stdlib only, matching the existing `immich_client.py` / `_webdock_photo_request` patterns). Each task is self-contained, has its own tests, and can be committed independently.

**Tech Stack:** FastAPI backend (`services/backend-api`), Postgres migrations (`db/migrations`), bash deploy scripts (`deploy/ecs`), GitHub Actions CI (`.github/workflows/ci.yml`), stdlib `urllib`/`hmac` for the OSS client.

---

## Already done — do not re-implement

Quick verification during planning found these `next-step-improvement-plan.md` items are **already implemented on `main`**. Do not duplicate them:

- 上传 MIME + 后缀双校验：`services/backend-api/app/main.py` has `ALLOWED_UPLOAD_MIMES` and `_validate_photo_upload()` (lines ~481-527), used by both `LocalPhotoStorage` and `WebDockPhotoStorage`.
- 前端统一鉴权：`services/public-web/memories/index.html` and `services/public-web/map/index.html` already use `window.AliECSAuth.API_BASE` / `getToken()` / `renderUserBadge()`, same as `services/public-web/couple/index.html`.
- `schema_migrations` table and idempotent migration tracking: already implemented in `deploy/ecs/migrate.sh`.
- `visibility` / `bucket_items.status` enum constraints: already enforced via `CHECK` constraints in `db/migrations/0003_couple_memory_phase2.sql`.

## Task 1: Report stale branch `codex/openclaw-metadata-prefix-20260612`

**Files:**
- Create: `docs/ops/branch-cleanup-notes.md`

- [ ] **Step 1: Write the findings as a short ops note**

```markdown
# Branch Cleanup Notes

## codex/openclaw-metadata-prefix-20260612

- Commit `c0ee2ec` ("fix openclaw metadata cleanup") targets
  `deploy/openclaw-bridge/openclaw_bridge.py` and
  `tests/test_openclaw_bridge.py`.
- `git diff origin/main origin/codex/openclaw-metadata-prefix-20260612 -- \
  deploy/openclaw-bridge/openclaw_bridge.py tests/test_openclaw_bridge.py`
  is empty: `main` already contains `OPENCLAW_METADATA_PREFIX_RE` and
  `parse_openclaw_metadata_prefix()` via a different commit.
- The branch's merge-base with `main` is `9c310b9`, which predates the
  couple-immich and formula-cost-simulation merges, so a raw `git diff
  origin/main...origin/codex/openclaw-metadata-prefix-20260612` shows large
  unrelated deletions. That is a stale-base artifact, not a real conflict.

**Conclusion:** this branch is superseded. No rebase or merge is needed.
Do not delete the branch as part of this plan — branch deletion requires
explicit confirmation from the repo owner (see global git safety rules).
Leave deletion as a manual follow-up.
```

- [ ] **Step 2: Commit**

```bash
git add docs/ops/branch-cleanup-notes.md
git commit -m "docs: record codex/openclaw-metadata-prefix-20260612 as superseded"
```

## Task 2: Structured JSON logging

**Files:**
- Create: `services/backend-api/app/logging_utils.py`
- Modify: `services/backend-api/app/main.py:1-45` (imports + middleware registration)
- Test: `tests/test_backend_logging.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backend_logging.py
from __future__ import annotations

import io
import json
import logging
import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_logging_utils():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import logging_utils

    return logging_utils


class JsonFormatterTests(unittest.TestCase):
    def test_format_emits_json_with_core_fields(self) -> None:
        logging_utils = load_logging_utils()
        formatter = logging_utils.JsonFormatter()
        record = logging.LogRecord(
            name="aliecs.request",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="request completed",
            args=None,
            exc_info=None,
        )
        record.request_id = "req-123"
        record.method = "GET"
        record.path = "/healthz"
        record.status_code = 200
        record.duration_ms = 1.5
        record.user = "tester"

        line = formatter.format(record)
        data = json.loads(line)

        self.assertEqual(data["message"], "request completed")
        self.assertEqual(data["level"], "INFO")
        self.assertEqual(data["request_id"], "req-123")
        self.assertEqual(data["method"], "GET")
        self.assertEqual(data["path"], "/healthz")
        self.assertEqual(data["status_code"], 200)
        self.assertEqual(data["duration_ms"], 1.5)
        self.assertEqual(data["user"], "tester")

    def test_configure_logging_attaches_json_handler(self) -> None:
        logging_utils = load_logging_utils()
        stream = io.StringIO()
        logger = logging_utils.configure_logging(name="aliecs.test", stream=stream)
        logger.info("hello", extra={"request_id": "r1"})

        line = stream.getvalue().strip().splitlines()[-1]
        data = json.loads(line)
        self.assertEqual(data["message"], "hello")
        self.assertEqual(data["request_id"], "r1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_backend_logging -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.logging_utils'`

- [ ] **Step 3: Implement `logging_utils.py`**

```python
# services/backend-api/app/logging_utils.py
"""Structured JSON logging helpers.

Keeps stdlib `logging` but renders one JSON object per line so log
aggregation (or plain `journalctl` greps) can filter by field instead of
parsing free-text messages.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import IO

_CORE_FIELDS = (
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "user",
    "route",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _CORE_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(name: str = "aliecs", stream: IO[str] | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, message: str, **fields: object) -> None:
    logger.info(message, extra=fields)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_backend_logging -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Wire a request-logging middleware into `main.py`**

In `services/backend-api/app/main.py`, add the import near the other `app.*` imports (after line 30, alongside `from app.recipes...` imports):

```python
from app.logging_utils import configure_logging, log_event
```

Then, immediately after the CORS middleware block (after the `app.add_middleware(CORSMiddleware, ...)` call, i.e. after the closing `)` that currently ends around line 77), add:

```python
_request_logger = configure_logging("aliecs.request")


@app.middleware("http")
async def _log_requests(request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - started) * 1000, 2)
    log_event(
        _request_logger,
        "request completed",
        request_id=request.headers.get("x-request-id", uuid.uuid4().hex),
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    return response
```

`time` and `uuid` are already imported at the top of `main.py` (lines 11 and 15), so no new imports beyond `logging_utils` are needed.

- [ ] **Step 6: Run the full backend test suite**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, no regressions (the middleware must not change response bodies/status codes — `TestClient` requests still return the same payloads).

- [ ] **Step 7: Commit**

```bash
git add services/backend-api/app/logging_utils.py services/backend-api/app/main.py tests/test_backend_logging.py
git commit -m "feat: add structured JSON request logging"
```

## Task 3: Upload disk watermark in `/healthz` + structured warning

**Files:**
- Modify: `services/backend-api/app/main.py:651-663` (`_warn_if_disk_high`)
- Modify: `services/backend-api/app/main.py:745-752` (`/healthz`)
- Test: `tests/test_backend_healthz_disk.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backend_healthz_disk.py
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_main():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import main

    return main


class HealthzDiskTests(unittest.TestCase):
    def test_healthz_reports_upload_disk_usage(self) -> None:
        main = load_main()
        client = TestClient(main.app)

        usage = main.shutil._ntuple_diskusage(total=1000, used=900, free=100)
        with patch.object(main.shutil, "disk_usage", return_value=usage):
            os.environ.pop("DATABASE_URL", None)
            resp = client.get("/healthz")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("upload_disk", body)
        self.assertEqual(body["upload_disk"]["percent"], 90.0)
        self.assertIn("path", body["upload_disk"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_backend_healthz_disk -v`
Expected: FAIL with `KeyError: 'upload_disk'`

- [ ] **Step 3: Add a shared disk-usage helper and use it from `/healthz` and `_warn_if_disk_high`**

Replace the body of `_warn_if_disk_high` (lines ~651-662 of `services/backend-api/app/main.py`):

```python
    def _warn_if_disk_high(self) -> None:
        info = _upload_disk_usage(self.base_dir)
        raw = os.getenv("UPLOAD_DISK_WARN_PCT", "").strip()
        if not raw:
            return
        try:
            threshold = float(raw)
        except ValueError:
            return
        if info["percent"] >= threshold:
            log_event(
                _request_logger,
                "upload disk usage high",
                path=info["path"],
                percent=info["percent"],
                threshold=threshold,
            )
```

Add the shared helper just above `class LocalPhotoStorage` (before line 625):

```python
def _upload_disk_usage(path: Path | str) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    percent = round(usage.used * 100 / usage.total, 1) if usage.total else 0.0
    return {
        "path": str(path),
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": percent,
    }
```

- [ ] **Step 4: Add the `upload_disk` field to `/healthz`**

Replace the body of `healthz()` (lines 745-752):

```python
@app.get("/healthz")
def healthz() -> dict[str, object]:
    db_ok, db_message = _db_ping()
    upload_dir = os.getenv("LOCAL_UPLOAD_DIR", "/tmp/aliecs-uploads")
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "backend-api",
        "database": {"ok": db_ok, "message": db_message},
        "upload_disk": _upload_disk_usage(upload_dir),
    }
```

`_upload_disk_usage` calls `shutil.disk_usage`, which requires the path to exist; `/tmp` and `/app/...` mount points always exist in the container, so no extra directory creation is needed for `/healthz` itself.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_backend_healthz_disk -v`
Expected: PASS

- [ ] **Step 6: Run the full backend test suite**

Run: `python -m unittest discover -s tests -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add services/backend-api/app/main.py tests/test_backend_healthz_disk.py
git commit -m "feat: report upload disk usage in /healthz and log high-usage warnings"
```

## Task 4: Migration `0013` — token revocation column + missing indexes

**Files:**
- Create: `db/migrations/0013_session_revocation_and_indexes.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 0013: session revocation column + missing indexes
ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version BIGINT NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_photos_memory_id ON photos(memory_id);
CREATE INDEX IF NOT EXISTS idx_couple_members_user_id ON couple_members(user_id);
```

- [ ] **Step 2: Verify it applies cleanly against a throwaway database**

Run:

```powershell
docker run --rm -e POSTGRES_PASSWORD=test -e POSTGRES_DB=aliecs_test -p 55432:5432 -d --name aliecs-migrate-test postgres:16-alpine
Start-Sleep -Seconds 3
foreach ($f in Get-ChildItem db/migrations/*.sql | Sort-Object Name) { Get-Content $f.FullName | docker exec -i aliecs-migrate-test psql -U postgres -d aliecs_test }
docker exec -i aliecs-migrate-test psql -U postgres -d aliecs_test -c "\d users" | Select-String token_version
docker rm -f aliecs-migrate-test
```

Expected: every migration applies without error, and `token_version` appears in the `users` table description.

- [ ] **Step 3: Commit**

```bash
git add db/migrations/0013_session_revocation_and_indexes.sql
git commit -m "feat: add token_version column and missing FK indexes"
```

## Task 5: JWT `jti` + `token_version`-based session revocation

**Files:**
- Modify: `services/backend-api/app/main.py:122-127` (`_token_secret`)
- Modify: `services/backend-api/app/main.py:1239-1290` (`auth_login`)
- Modify: `services/backend-api/app/main.py:284-298` (`get_current_user`, `require_admin`)
- Modify: `services/backend-api/app/main.py` (add admin revoke endpoint near other `/v1/admin` routes)
- Test: `tests/test_backend_session_revocation.py`

This task depends on Task 4's migration being present (the new code queries `users.token_version`). It does not depend on Tasks 2/3.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backend_session_revocation.py
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_main():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import main

    return main


class TokenRevocationTests(unittest.TestCase):
    def test_encode_token_includes_jti_and_tv(self) -> None:
        main = load_main()
        with patch.dict("os.environ", {"AUTH_TOKEN_SECRET": "x" * 32, "ENV": "dev"}):
            payload = {
                "sub": "alice",
                "uid": 1,
                "roles": [],
                "permissions": [],
                "tv": 1,
                "iat": 0,
                "exp": 9999999999,
            }
            token = main._encode_token(payload)
            decoded = main._decode_token(token)

        self.assertIn("jti", decoded)
        self.assertEqual(decoded["tv"], 1)

    def test_decode_token_with_stale_token_version_is_rejected(self) -> None:
        main = load_main()
        with patch.dict("os.environ", {"AUTH_TOKEN_SECRET": "x" * 32, "ENV": "dev"}):
            payload = {
                "sub": "alice",
                "uid": 1,
                "roles": [],
                "permissions": [],
                "tv": 1,
                "iat": 0,
                "exp": 9999999999,
                "jti": "abc",
            }
            token = main._encode_token(payload)

            with patch.object(main, "_current_token_version", return_value=2):
                with self.assertRaises(main.HTTPException) as ctx:
                    main.get_current_user(authorization=f"Bearer {token}")

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("revoked", ctx.exception.detail)

    def test_token_secret_rejects_short_secret_in_prod(self) -> None:
        main = load_main()
        with patch.dict("os.environ", {"AUTH_TOKEN_SECRET": "short", "ENV": "prod"}, clear=False):
            with self.assertRaises(main.HTTPException) as ctx:
                main._token_secret()

        self.assertEqual(ctx.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_backend_session_revocation -v`
Expected: FAIL — `_current_token_version` does not exist yet, and short secrets are not rejected.

- [ ] **Step 3: Tighten `_token_secret()` to reject short secrets in prod**

Replace lines 122-127 of `services/backend-api/app/main.py`:

```python
def _token_secret() -> str:
    secret = os.getenv("AUTH_TOKEN_SECRET", "change-this-in-production")
    env_name = os.getenv("ENV", "dev")
    if env_name == "prod" and secret == "change-this-in-production":
        raise HTTPException(status_code=500, detail="AUTH_TOKEN_SECRET must be changed in production")
    if env_name == "prod" and len(secret) < 32:
        raise HTTPException(status_code=500, detail="AUTH_TOKEN_SECRET must be at least 32 characters in production")
    return secret
```

- [ ] **Step 4: Add `_current_token_version` helper near `_user_roles_permissions`**

Find `_user_roles_permissions` (search for `def _user_roles_permissions`) and add this helper directly after it:

```python
def _current_token_version(user_id: int) -> int | None:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT token_version FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    return int(row[0])
```

Returning `None` on DB errors keeps `get_current_user` working even if the DB is briefly unreachable — see Step 5, which only rejects when it gets a *definite* mismatch.

- [ ] **Step 5: Make `get_current_user` check `tv` against the DB**

Replace lines 284-286:

```python
def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _extract_bearer(authorization)
    payload = _decode_token(token)
    uid = payload.get("uid")
    if uid is not None and "tv" in payload:
        current = _current_token_version(int(uid))
        if current is not None and int(payload["tv"]) != current:
            raise HTTPException(status_code=401, detail="token revoked")
    return payload
```

- [ ] **Step 6: Add `jti` and `tv` to the login payload**

In `auth_login` (around line 1247), extend the `SELECT` to also fetch `token_version`:

```python
            cur.execute(
                """
                SELECT id, username, display_name, password_hash, status, is_admin, token_version
                FROM users
                WHERE username = %s
                """,
                (body.username,),
            )
```

Then update the payload construction (around lines 1265-1274):

```python
            now = int(time.time())
            payload = {
                "sub": row[1],
                "uid": user_id,
                "display_name": row[2],
                "roles": roles,
                "permissions": permissions,
                "tv": int(row[6]),
                "jti": uuid.uuid4().hex,
                "iat": now,
                "exp": now + _token_ttl_seconds(),
            }
```

`uuid` is already imported at the top of `main.py` (line 15).

- [ ] **Step 7: Add an admin "revoke sessions" endpoint**

Find the existing admin user-management routes (search for `require_admin` near user endpoints, e.g. `@app.post("/v1/admin/users` or similar — if no such prefix exists yet, add this route directly after `auth_login`). Add:

```python
@app.post("/v1/admin/users/{user_id}/revoke-sessions")
def admin_revoke_user_sessions(user_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET token_version = token_version + 1, updated_at = NOW() WHERE id = %s RETURNING token_version",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="user not found")
        conn.commit()

    _audit(user.get("sub"), "admin.users.revoke_sessions", "users", str(user_id))
    return {"user_id": user_id, "token_version": int(row[0])}
```

- [ ] **Step 8: Run test to verify it passes**

Run: `python -m unittest tests.test_backend_session_revocation -v`
Expected: PASS (3 tests)

- [ ] **Step 9: Run the full backend test suite**

Run: `python -m unittest discover -s tests -v`
Expected: PASS. If any existing test constructs a login payload by hand and asserts on its exact keys, update that assertion to also expect `jti` and `tv`.

- [ ] **Step 10: Commit**

```bash
git add services/backend-api/app/main.py tests/test_backend_session_revocation.py
git commit -m "feat: add jti/token_version session revocation and admin revoke endpoint"
```

## Task 6: `deploy.sh` image-tag validation + `migrate.sh` target-info printing

**Files:**
- Modify: `deploy/ecs/deploy.sh:1-12`
- Modify: `deploy/ecs/migrate.sh` (after the existing pre-flight checks, before "执行 ... 下的 SQL")
- Create: `deploy/ecs/tests/test_validate_tag.sh`

- [ ] **Step 1: Write the failing validation test script**

```bash
#!/usr/bin/env bash
# deploy/ecs/tests/test_validate_tag.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_SH="$ROOT_DIR/deploy.sh"

assert_rejects() {
  local tag="$1"
  if "$DEPLOY_SH" "$tag" >/dev/null 2>&1; then
    echo "FAIL: expected '$tag' to be rejected" >&2
    exit 1
  fi
}

assert_accepts_format() {
  # deploy.sh exits early on missing release-meta.env, but only AFTER tag
  # validation. A tag-format failure must happen before the meta-file check,
  # so we look for the tag-format error message specifically.
  local tag="$1"
  local output
  output="$("$DEPLOY_SH" "$tag" 2>&1 || true)"
  if echo "$output" | grep -q "镜像标签格式错误"; then
    echo "FAIL: expected '$tag' to pass format validation, got: $output" >&2
    exit 1
  fi
}

assert_rejects "latest"
assert_rejects "v1.2"
assert_rejects "1.2.3"
assert_accepts_format "v1.2.3"
assert_accepts_format "v1.2.3-rc.1"

echo "OK"
```

Make it executable:

```bash
chmod +x deploy/ecs/tests/test_validate_tag.sh
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash deploy/ecs/tests/test_validate_tag.sh`
Expected: FAIL — `assert_rejects "latest"` fails because `deploy.sh` currently accepts any tag (it will instead fail later on the missing `release-meta.env`, with exit code 1 but the wrong message, or `assert_accepts_format` may pass vacuously). Either way, run it once to confirm the new format-error message `镜像标签格式错误` is not yet produced.

- [ ] **Step 3: Add tag format validation to `deploy.sh`**

Replace lines 1-12 of `deploy/ecs/deploy.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "用法：$0 <镜像标签>" >&2
  exit 1
fi

IMAGE_TAG="$1"

if [[ ! "$IMAGE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]]; then
  echo "[部署] 镜像标签格式错误：$IMAGE_TAG（必须是 vX.Y.Z 或 vX.Y.Z-rc.N）" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
META_FILE="$ROOT_DIR/release-meta.env"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `bash deploy/ecs/tests/test_validate_tag.sh`
Expected: `OK`

- [ ] **Step 5: Print masked target info before running migrations**

In `deploy/ecs/migrate.sh`, after the existing guard clauses (after the `if [[ ! -d "$MIGRATIONS_DIR" ]]; then ... fi` block, before `echo "[迁移] 先确保 postgres 已启动"`), add:

```bash
db_host="${DATABASE_URL#*@}"
db_host="${db_host%%/*}"
echo "[迁移] 目标：host=${db_host} db=${POSTGRES_DB} 镜像tag=${IMAGE_TAG:-unset}"
```

This prints the Postgres host:port and database name without the embedded credentials (everything before `@` in `DATABASE_URL` is stripped), plus the image tag if `IMAGE_TAG` is exported by the caller.

- [ ] **Step 6: Verify shell syntax**

Run:

```bash
bash -n deploy/ecs/deploy.sh
bash -n deploy/ecs/migrate.sh
bash -n deploy/ecs/tests/test_validate_tag.sh
```

Expected: no output (all three parse cleanly).

- [ ] **Step 7: Commit**

```bash
git add deploy/ecs/deploy.sh deploy/ecs/migrate.sh deploy/ecs/tests/test_validate_tag.sh
git commit -m "feat: validate image tag format and print masked migration target"
```

## Task 7: Real OSS photo storage driver

**Files:**
- Create: `services/backend-api/app/oss_client.py`
- Modify: `services/backend-api/app/main.py:665-670` (`OssPhotoStorage`)
- Modify: `deploy/ecs/runtime.env.example:51-54` (OSS placeholders — add `OSS_REGION`)
- Modify: `docs/env-matrix.md` (OSS row — mention `OSS_REGION`)
- Test: `tests/test_couple_oss_photo_storage.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_couple_oss_photo_storage.py
from __future__ import annotations

import asyncio
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.datastructures import Headers, UploadFile


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_main():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import main

    return main


class FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return b""


class OssClientSigningTests(unittest.TestCase):
    def test_canonical_resource_and_headers(self) -> None:
        main = load_main()
        from app.oss_client import OssClient, OssConfig

        config = OssConfig(
            endpoint="oss-cn-hangzhou.aliyuncs.com",
            bucket="aliecs-photos",
            access_key_id="AKIDEXAMPLE",
            access_key_secret="secret",
        )
        client = OssClient(config)

        headers = client._signed_headers("PUT", "couple/abc.png", content_type="image/png")
        self.assertIn("Authorization", headers)
        self.assertTrue(headers["Authorization"].startswith("OSS AKIDEXAMPLE:"))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertIn("Date", headers)

    def test_object_url(self) -> None:
        main = load_main()
        from app.oss_client import OssClient, OssConfig

        config = OssConfig(
            endpoint="oss-cn-hangzhou.aliyuncs.com",
            bucket="aliecs-photos",
            access_key_id="AKIDEXAMPLE",
            access_key_secret="secret",
        )
        client = OssClient(config)
        self.assertEqual(
            client.object_url("couple/abc.png"),
            "https://aliecs-photos.oss-cn-hangzhou.aliyuncs.com/couple/abc.png",
        )


class OssPhotoStorageTests(unittest.TestCase):
    def test_save_uploads_and_returns_oss_urls(self) -> None:
        main = load_main()

        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request)
            return FakeResponse(status=200)

        env = {
            "STORAGE_DRIVER": "oss",
            "OSS_ENDPOINT": "oss-cn-hangzhou.aliyuncs.com",
            "OSS_BUCKET": "aliecs-photos",
            "OSS_ACCESS_KEY_ID": "AKIDEXAMPLE",
            "OSS_ACCESS_KEY_SECRET": "secret",
            "MAX_UPLOAD_MB": "15",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(main.urllib.request, "urlopen", fake_urlopen):
            storage = main.photo_storage()
            self.assertEqual(storage.driver, "oss")

            upload = UploadFile(
                file=io.BytesIO(b"\x89PNG\r\n\x1a\nphoto"),
                filename="memory.png",
                headers=Headers({"content-type": "image/png"}),
            )
            result = asyncio.run(storage.save(upload))

        self.assertEqual(result["storage_driver"], "oss")
        self.assertTrue(result["display_url"].startswith("https://aliecs-photos.oss-cn-hangzhou.aliyuncs.com/"))
        self.assertEqual(result["display_url"], result["thumbnail_url"])
        self.assertTrue(len(calls) >= 1)
        self.assertEqual(calls[0].get_method(), "PUT")

    def test_save_without_config_raises_501(self) -> None:
        main = load_main()
        env = {"STORAGE_DRIVER": "oss"}
        for key in ("OSS_ENDPOINT", "OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"):
            env[key] = ""
        with patch.dict(os.environ, env, clear=False):
            storage = main.photo_storage()
            upload = UploadFile(
                file=io.BytesIO(b"\x89PNG\r\n\x1a\nphoto"),
                filename="memory.png",
                headers=Headers({"content-type": "image/png"}),
            )
            with self.assertRaises(main.HTTPException) as ctx:
                asyncio.run(storage.save(upload))

        self.assertEqual(ctx.exception.status_code, 501)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_couple_oss_photo_storage -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.oss_client'`

- [ ] **Step 3: Implement `oss_client.py` (Aliyun OSS V1 signing, stdlib only)**

```python
# services/backend-api/app/oss_client.py
"""Minimal Aliyun OSS V1-signature client.

Stdlib only (urllib + hmac + hashlib + base64), matching the style of
`immich_client.py` and `_webdock_photo_request`. Implements just enough of
the OSS REST API for photo storage: PUT object, DELETE object, and building
public object URLs for the virtual-hosted-style bucket domain.

Reference: https://help.aliyun.com/document_detail/31951.html (V1 signature)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import formatdate


@dataclass
class OssConfig:
    endpoint: str
    bucket: str
    access_key_id: str
    access_key_secret: str
    timeout_seconds: float = 30.0

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key_id and self.access_key_secret)


def config_from_env() -> OssConfig:
    return OssConfig(
        endpoint=os.getenv("OSS_ENDPOINT", "").strip(),
        bucket=os.getenv("OSS_BUCKET", "").strip(),
        access_key_id=os.getenv("OSS_ACCESS_KEY_ID", "").strip(),
        access_key_secret=os.getenv("OSS_ACCESS_KEY_SECRET", "").strip(),
        timeout_seconds=float(os.getenv("OSS_TIMEOUT_SECONDS", "30")),
    )


class OssError(Exception):
    pass


class OssClient:
    def __init__(self, config: OssConfig) -> None:
        self.config = config

    def object_url(self, key: str) -> str:
        return f"https://{self.config.bucket}.{self.config.endpoint}/{key}"

    def _signed_headers(self, method: str, key: str, *, content_type: str = "") -> dict[str, str]:
        date = formatdate(usegmt=True)
        resource = f"/{self.config.bucket}/{key}"
        string_to_sign = f"{method}\n\n{content_type}\n{date}\n{resource}"
        signature = base64.b64encode(
            hmac.new(
                self.config.access_key_secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("utf-8")
        headers = {
            "Date": date,
            "Authorization": f"OSS {self.config.access_key_id}:{signature}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def put_object(self, key: str, content: bytes, content_type: str) -> None:
        headers = self._signed_headers("PUT", key, content_type=content_type)
        request = urllib.request.Request(
            self.object_url(key), data=content, headers=headers, method="PUT"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise OssError(f"OSS PUT {key} failed: HTTP {exc.code}: {body[:300]}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise OssError(f"OSS PUT {key} failed: {exc}") from exc

    def delete_object(self, key: str) -> None:
        headers = self._signed_headers("DELETE", key)
        request = urllib.request.Request(self.object_url(key), headers=headers, method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return
            body = exc.read().decode("utf-8", errors="replace")
            raise OssError(f"OSS DELETE {key} failed: HTTP {exc.code}: {body[:300]}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise OssError(f"OSS DELETE {key} failed: {exc}") from exc
```

- [ ] **Step 4: Implement `OssPhotoStorage` in `main.py`**

Replace lines 665-670 of `services/backend-api/app/main.py`:

```python
class OssPhotoStorage(PhotoStorage):
    driver = "oss"

    def __init__(self) -> None:
        from app.oss_client import OssClient, config_from_env

        config = config_from_env()
        if not config.enabled:
            self._client = None
        else:
            self._client = OssClient(config)

    async def save(self, file: UploadFile) -> dict[str, str]:
        content = await file.read()
        if self._client is None:
            raise HTTPException(status_code=501, detail="OSS storage is not configured in this build")

        ext, mime = _validate_photo_upload(file.filename, file.content_type, content)
        key = f"couple/{uuid.uuid4().hex}{ext}"
        from app.oss_client import OssError

        try:
            self._client.put_object(key, content, mime)
        except OssError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        public_url = self._client.object_url(key)
        return {
            "original_storage_url": f"oss:{key}",
            "display_url": public_url,
            "thumbnail_url": public_url,
            "storage_driver": self.driver,
        }

    def delete(self, original_storage_url: str | None) -> None:
        if not original_storage_url or not original_storage_url.startswith("oss:"):
            return
        if self._client is None:
            return
        key = original_storage_url.split(":", 1)[1]
        from app.oss_client import OssError

        try:
            self._client.delete_object(key)
        except OssError:
            pass
```

`uuid` is already imported at the top of `main.py`.

- [ ] **Step 5: Add `OSS_REGION` placeholder and document it**

In `deploy/ecs/runtime.env.example`, after line 54 (`OSS_ACCESS_KEY_SECRET=`), add:

```
OSS_TIMEOUT_SECONDS=30
```

In `docs/env-matrix.md`, update the existing OSS row (around line 50-51) to mention the timeout var — append a new row directly below the existing `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` row:

```markdown
| `OSS_TIMEOUT_SECONDS` | OSS 请求超时（秒） | 可选，默认 30 | 网络异常时请求可能挂起更久 |
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m unittest tests.test_couple_oss_photo_storage -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Run the full backend test suite**

Run: `python -m unittest discover -s tests -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add services/backend-api/app/oss_client.py services/backend-api/app/main.py deploy/ecs/runtime.env.example docs/env-matrix.md tests/test_couple_oss_photo_storage.py
git commit -m "feat: implement Aliyun OSS photo storage driver"
```

## Task 8: CI migration dry-run + backend smoke test

**Files:**
- Modify: `.github/workflows/ci.yml`
- Create: `tests/test_backend_smoke.py`

This task depends on Task 4's migration file existing (so the dry-run job has something to apply) but does not depend on Tasks 5-7.

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/test_backend_smoke.py
"""Backend smoke test: login -> features -> couple access -> healthz.

Skips itself (instead of failing) when DATABASE_URL is not configured, so it
only runs meaningfully in the CI migration-dry-run job which provisions a
real Postgres service container and applies all migrations first.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"


def load_main():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    backend_root = str(BACKEND_ROOT)
    sys.path[:] = [item for item in sys.path if item != backend_root]
    sys.path.insert(0, backend_root)
    from app import main

    return main


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "smoke test requires a real DATABASE_URL")
class BackendSmokeTests(unittest.TestCase):
    def test_healthz_is_ok_against_real_db(self) -> None:
        main = load_main()
        client = TestClient(main.app)

        resp = client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["database"]["ok"])

    def test_login_with_bootstrap_admin_then_features(self) -> None:
        main = load_main()
        client = TestClient(main.app)

        username = os.environ["ADMIN_BOOTSTRAP_USERNAME"]
        password = os.environ["ADMIN_BOOTSTRAP_PASSWORD"]

        resp = client.post("/v1/auth/login", json={"username": username, "password": password})
        self.assertEqual(resp.status_code, 200, resp.text)
        token = resp.json()["token"]

        headers = {"Authorization": f"Bearer {token}"}
        resp = client.get("/v1/features", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = client.get("/couple/access", headers=headers)
        self.assertIn(resp.status_code, (200, 403))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it skips locally**

Run: `python -m unittest tests.test_backend_smoke -v`
Expected: both tests reported as `skipped` (no `DATABASE_URL` set locally) — this confirms the skip guard works and the module imports cleanly.

- [ ] **Step 3: Verify `/v1/features` and `/couple/access` route names**

Run:

```powershell
Select-String -Path services/backend-api/app/main.py -Pattern '@app\.get\("/v1/features"|@app\.get\("/couple/access"'
```

Expected: both routes exist. If either path differs, update `tests/test_backend_smoke.py` Step 1 to use the actual path before continuing.

- [ ] **Step 4: Add a `migration-dry-run` job to CI**

In `.github/workflows/ci.yml`, add a new top-level job after the existing `validate` job (same indentation level as `validate:`):

```yaml
  migration-dry-run:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: aliecs
          POSTGRES_PASSWORD: aliecs
          POSTGRES_DB: aliecs
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U aliecs -d aliecs"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r services/backend-api/requirements.txt
          python -m pip install pytest==9.0.3

      - name: Apply all migrations
        run: |
          for f in db/migrations/*.sql; do
            echo "Applying $f"
            PGPASSWORD=aliecs psql -h 127.0.0.1 -U aliecs -d aliecs -f "$f"
          done

      - name: Run backend smoke test
        env:
          DATABASE_URL: postgresql://aliecs:aliecs@127.0.0.1:5432/aliecs
          AUTH_TOKEN_SECRET: ci-test-secret-please-rotate-0123456789
          ENV: dev
          ADMIN_BOOTSTRAP_USERNAME: ci-admin
          ADMIN_BOOTSTRAP_PASSWORD: ci-admin-password-0123456789
          ADMIN_BOOTSTRAP_DISPLAY_NAME: CI Admin
        run: |
          python -m unittest tests.test_backend_smoke -v
```

Adjust the `psql` client install if the `ubuntu-latest` runner does not have it preinstalled — if Step 4's run fails with `psql: command not found`, add `sudo apt-get update && sudo apt-get install -y postgresql-client` as a step before "Apply all migrations".

- [ ] **Step 5: Verify CI YAML syntax**

Run:

```powershell
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml', encoding='utf-8'))"
```

Expected: no output (valid YAML). If `pyyaml` is not installed locally, run `python -m pip install pyyaml` first — this is a throwaway local check, not a new project dependency.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml tests/test_backend_smoke.py
git commit -m "ci: add migration dry-run job and backend smoke test"
```

---

## Self-Review

**Spec coverage:**

- 1.1 JWT/jti/token_version revocation + `AUTH_TOKEN_SECRET` length check → Tasks 4, 5.
- 1.2 upload MIME/extension validation → already done (see "Already done"); disk watermark → Task 3.
- 1.3 `deploy.sh` tag validation + migration target printing → Task 6.
- 2.2 missing indexes (`photos.memory_id`, `couple_members.user_id`) → Task 4. Enum constraints already done.
- 2.3 frontend auth unification → already done.
- 3.1 OSS storage driver → Task 7.
- 3.2 structured logging → Task 2; `/healthz` disk reporting → Task 3.
- 3.3 CI migration dry-run + smoke test → Task 8.
- Stale branch report → Task 1.

Items intentionally **out of scope** for this plan (larger product features, not "hardening"): 2.1 Couple Space management API (create space / member management / leave), Memory archive/unarchive, Share Link create/revoke; 2.3 `admin-ui` RBAC/audit visualization page; 2.3 `public-web` dashboard timeline/anniversary/wishlist blocks. These need their own brainstorming + plan if wanted next.

**Placeholder scan:** no TODO/TBD/"add appropriate" phrases; every step has complete code or an exact command.

**Type/signature consistency:**

- `_upload_disk_usage(path: Path | str) -> dict[str, Any]` is defined once (Task 3) and used by both `_warn_if_disk_high` and `/healthz`.
- `_current_token_version(user_id: int) -> int | None` (Task 5) matches its call site in `get_current_user`.
- `OssConfig` / `OssClient` / `OssError` (Task 7) are used consistently between `oss_client.py` and `OssPhotoStorage`.
- Token payload now always has `tv` and `jti` after Task 5 — Task 5 Step 9 calls out updating any existing test that hand-builds a payload and asserts on its exact key set.

---

## Operator Prompt Template (Codex, unattended)

Use this prompt to hand the whole plan to Codex for non-stop execution. Copy it verbatim, filling in nothing — every path and command is already absolute/relative to the repo root.

```text
You are executing an approved implementation plan end-to-end without stopping
for confirmation, except at the Hard Stop Conditions below.

Plan file: docs/superpowers/plans/2026-06-12-app-hardening-backlog.md
Repo root: this checkout of AliECS (origin/main = 937bee7 at plan time)

Rules:
1. Work through Task 1 -> Task 8 in order. Each task lists Files, then
   checkbox steps. Execute every step's code/commands exactly as written --
   do not paraphrase or "improve" the code beyond what each step specifies.
2. After each task's final commit step, run:
     python -m unittest discover -s tests -v
   If it fails, fix the regression before moving to the next task. Do not
   skip ahead with a failing suite.
3. Tasks 1-3 and 6-8 have no inter-task dependency beyond Task 4 -> Task 5
   and Task 4 -> Task 8 (the migration file must exist first). If you want to
   parallelize across worktrees, Task 4 must land before Tasks 5 and 8.
4. Make one git commit per task step that says "Commit" -- do not batch
   multiple tasks into one commit.
5. Do not touch files outside this plan's "Files" lists.
6. Do not add new runtime dependencies. Everything here is stdlib + the
   project's existing requirements.txt.

Hard Stop Conditions (stop and report instead of proceeding):
- Any step's "Run test to verify it fails/passes" produces an error message
  that doesn't match what the plan describes, AND you cannot resolve it by
  re-reading the referenced source file (e.g. a referenced line range has
  drifted because main.py changed since the plan was written).
- `git push` is requested anywhere -- this plan only requires local commits.
  Do not push.
- Any step would require a secret, credential, or production hostname that
  is not already a placeholder in deploy/ecs/runtime.env.example.

When all 8 tasks are committed and the full suite passes, report:
- Which tasks completed.
- Final `git log --oneline -n 10`.
- Output of `python -m unittest discover -s tests -v` (last 20 lines).
```
