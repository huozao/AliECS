# 全设备版本看板（version-inventory）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 /health 看板显示每台设备各容器/组件的当前版本与上游最新版本并标出落后项，每周一飞书推送更新摘要，把 OpenClaw/Immich 等"装完就忘"组件的版本跟进变成机器化的定期人工确认。

**Architecture:** 复用备份看板既有管道——三机每日 systemd timer 采集全量容器版本+apt 计数，POST 到 backend；backend 存库、按组件表匹配、每日查 GitHub/Docker Hub 上游、出 `/v1/ops/versions` 看板数据、每周经飞书 im API 直发摘要。设备心跳复用 `backup_policies` 的 stale 告警（零新告警代码）。

**Tech Stack:** FastAPI + psycopg3 + PostgreSQL（backend-api）；纯 HTML/JS（public-web /health 页）；POSIX bash + jq + systemd timer（infra 采集脚本）；SOPS+age（密钥）。

## Global Constraints

- 后端路由按域拆分：新文件 `services/backend-api/app/routers/versions.py`，在 `app/main.py` 装配（`include_router`），照 `backups.py` 的模式（`from app.core import _conn, require_admin`）。
- token 校验复用 `BACKUP_REPORT_TOKEN`（backend 已注入）；端点校验函数照抄 `_require_backup_report_token` 的 `hmac.compare_digest` 写法。
- 迁移文件按序号递增：下一个是 `db/migrations/0037_version_inventory.sql`。**心跳 policy 必须在迁移里 INSERT 进 `backup_policies`**，否则 report 端点校验 policy_code 报 404（历史踩坑）。
- 测试用 `unittest`（非 pytest 类），放 `tests/`，照 `tests/test_backup_dashboard.py`：`sys.path.insert(0, BACKEND_ROOT)` 后 import `app.routers.*`。运行 `python -m unittest tests.test_xxx -v`（Windows 上目录形式 `node --test` 会失败的教训不适用于此，Python unittest 用模块路径）。
- 不新增后端第三方依赖：上游查询用标准库 `urllib.request`（不引 httpx/requests）。
- 自家镜像（AliECS 业务镜像、`ghcr.io/huozao/webdock`、`ghcr.io/huozao/openclaw-bridge`）`upstream_source='none'`、`family='own'`，只做两机 tag 一致性核对，不查上游。
- infra 采集脚本照 `infra/backup/docker-image-maintenance.sh` 模板：`set -Eeuo pipefail`、spool 目录离线重放、`curl -fsS --max-time`、token 从 SOPS 解出经 Environment 注入。
- 两个 PR：**PR-A（AliECS 仓库）** = 迁移+端点+看板+digest；**PR-B（infra 仓库）** = 采集脚本+timer+token 分发+render 接线。PR-A 先合并上线，PR-B 后行。
- 提交信息结尾：
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_0199jZtnm6SpUPGLG5kg4nRU
  ```

---

# PR-A：AliECS 仓库（worktree `AliECS-version-inventory`，分支 `feat/version-inventory`）

## Task 1: 数据模型迁移 + 心跳 policy 种子

**Files:**
- Create: `db/migrations/0037_version_inventory.sql`
- Test: `tests/test_version_inventory_migration.py`

**Interfaces:**
- Produces: 表 `version_components(component_key PK, display_name, kind, match_images text[], devices text[], upstream_source, upstream_ref, version_pattern, pin_note, family, sort_order, active)`；`version_reports(id, device, image, tag, digest, extra_json jsonb, reported_at)`；`version_upstream_state(component_key PK, latest_version, release_url, checked_at, check_status, check_error)`；`backup_policies` 新增三行心跳 `version-inventory-{aliecs,webdock1,webdock2}`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_version_inventory_migration.py
from __future__ import annotations
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "db" / "migrations" / "0037_version_inventory.sql"


class VersionInventoryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = MIG.read_text(encoding="utf-8")

    def test_creates_three_tables(self) -> None:
        for t in ("version_components", "version_reports", "version_upstream_state"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {t}", self.sql)

    def test_seeds_heartbeat_policies_for_each_device(self) -> None:
        for code in ("version-inventory-aliecs", "version-inventory-webdock1", "version-inventory-webdock2"):
            self.assertIn(f"'{code}'", self.sql)
        self.assertIn("ON CONFLICT (code) DO UPDATE", self.sql)

    def test_seeds_pain_point_components(self) -> None:
        for key in ("openclaw", "immich-server", "postgres-aliecs"):
            self.assertIn(f"'{key}'", self.sql)

    def test_pins_postgres_major_version(self) -> None:
        # postgres 锁大版本，避免误报"该升 17"
        self.assertIn("^16", self.sql)

    def test_own_images_have_no_upstream(self) -> None:
        self.assertIn("'own'", self.sql)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_version_inventory_migration -v`
Expected: FAIL（文件不存在 / 断言不满足）

- [ ] **Step 3: 写迁移**

```sql
-- db/migrations/0037_version_inventory.sql
-- 全设备版本看板：组件登记表 + 上报表 + 上游对比表。复用备份看板 backup_policies 做设备心跳。
CREATE TABLE IF NOT EXISTS version_components (
    component_key   text PRIMARY KEY,
    display_name    text NOT NULL,
    kind            text NOT NULL DEFAULT 'docker-image',  -- docker-image | apt-summary | binary
    match_images    text[] NOT NULL DEFAULT '{}',
    devices         text[],                                 -- NULL=任意设备；用于区分同名镜像跨机（postgres）
    upstream_source text NOT NULL DEFAULT 'none',           -- github-release | dockerhub | none
    upstream_ref    text,                                   -- 'immich-app/immich' | 'library/postgres'
    version_pattern text,                                   -- 版本提取/比较正则；postgres 锁 '^16\.'
    pin_note        text,
    family          text NOT NULL DEFAULT 'third-party',    -- own | third-party | os
    sort_order      int NOT NULL DEFAULT 100,
    active          boolean NOT NULL DEFAULT TRUE,
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS version_reports (
    id          bigserial PRIMARY KEY,
    device      text NOT NULL,
    image       text NOT NULL,
    tag         text,
    digest      text,
    extra_json  jsonb NOT NULL DEFAULT '{}'::jsonb,
    reported_at timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_version_reports_device_time
    ON version_reports (device, reported_at DESC);

CREATE TABLE IF NOT EXISTS version_upstream_state (
    component_key text PRIMARY KEY REFERENCES version_components(component_key) ON DELETE CASCADE,
    latest_version text,
    release_url    text,
    checked_at     timestamptz,
    check_status   text,       -- ok | error
    check_error    text
);

-- 组件种子（来源：2026-07-17 实测容器清单）
INSERT INTO version_components
    (component_key, display_name, kind, match_images, devices, upstream_source, upstream_ref, version_pattern, pin_note, family, sort_order)
VALUES
    ('openclaw', 'OpenClaw 网关', 'docker-image', '{ghcr.io/openclaw/openclaw}', '{aliecs}',
     'github-release', 'openclaw/openclaw', NULL, '镜像按 sha256 锁定，实际版本取自 extra_json（容器内 exec 采集）', 'third-party', 10),
    ('authelia', 'Authelia SSO', 'docker-image', '{ghcr.io/authelia/authelia}', '{aliecs}',
     'github-release', 'authelia/authelia', NULL, NULL, 'third-party', 20),
    ('lldap', 'lldap 账号目录', 'docker-image', '{lldap/lldap}', '{aliecs}',
     'github-release', 'lldap/lldap', NULL, NULL, 'third-party', 21),
    ('postgres-aliecs', 'PostgreSQL（生产）', 'docker-image', '{postgres}', '{aliecs}',
     'dockerhub', 'library/postgres', '^16\.', '锁 16 大版本，只比对 16.x 内小版本升级', 'third-party', 30),
    ('immich-server', 'Immich 服务端', 'docker-image', '{ghcr.io/immich-app/immich-server}', '{webdock1}',
     'github-release', 'immich-app/immich', NULL, 'CVE 活跃，重点跟进', 'third-party', 40),
    ('immich-ml', 'Immich 机器学习', 'docker-image', '{ghcr.io/immich-app/immich-machine-learning}', '{webdock1}',
     'github-release', 'immich-app/immich', NULL, '版本随 immich-server 同步', 'third-party', 41),
    ('immich-postgres', 'Immich 数据库', 'docker-image', '{ghcr.io/immich-app/postgres,tensorchord/pgvecto-rs}', '{webdock1}',
     'none', NULL, NULL, '跟随 Immich 官方 compose 指定版本，不独立升级', 'third-party', 42),
    ('immich-redis', 'Immich Redis', 'docker-image', '{redis,valkey/valkey,docker.io/valkey/valkey}', '{webdock1}',
     'none', NULL, NULL, '跟随 Immich 官方 compose', 'third-party', 43),
    ('adventurelog-frontend', 'AdventureLog 前端', 'docker-image', '{ghcr.io/seanmorley15/adventurelog-frontend}', '{webdock1}',
     'github-release', 'seanmorley15/AdventureLog', NULL, NULL, 'third-party', 50),
    ('adventurelog-backend', 'AdventureLog 后端', 'docker-image', '{ghcr.io/seanmorley15/adventurelog-backend}', '{webdock1}',
     'github-release', 'seanmorley15/AdventureLog', NULL, NULL, 'third-party', 51),
    ('gokapi', 'Gokapi 文件分享', 'docker-image', '{f0rc3/gokapi,ghcr.io/forceu/gokapi}', '{webdock1}',
     'github-release', 'forceu/gokapi', NULL, NULL, 'third-party', 60),
    ('sing-box', 'sing-box', 'docker-image', '{ghcr.io/sagernet/sing-box}', '{aliecs}',
     'github-release', 'SagerNet/sing-box', NULL, NULL, 'third-party', 70),
    ('aliecs-services', 'AliECS 业务镜像', 'docker-image',
     '{ghcr.io/huozao/backend-api,ghcr.io/huozao/public-web,ghcr.io/huozao/admin-ui}', '{aliecs}',
     'none', NULL, NULL, '自家镜像，release 自动部署最新，无需上游对比', 'own', 80),
    ('openclaw-bridge', 'OpenClaw Bridge', 'docker-image', '{ghcr.io/huozao/openclaw-bridge}', '{aliecs}',
     'none', NULL, NULL, '自家镜像，手动 cutover', 'own', 81),
    ('webdock', 'WebDock 节点镜像', 'docker-image', '{ghcr.io/huozao/webdock}', '{webdock1,webdock2}',
     'none', NULL, NULL, '自家镜像，两机应保持同 tag（一致性核对）', 'own', 82),
    ('apt-summary', 'APT 可升级包', 'apt-summary', '{}', NULL,
     'none', NULL, NULL, '仅显示可升级数量与 security 数', 'os', 90)
ON CONFLICT (component_key) DO UPDATE SET
    display_name=EXCLUDED.display_name, kind=EXCLUDED.kind, match_images=EXCLUDED.match_images,
    devices=EXCLUDED.devices, upstream_source=EXCLUDED.upstream_source, upstream_ref=EXCLUDED.upstream_ref,
    version_pattern=EXCLUDED.version_pattern, pin_note=EXCLUDED.pin_note, family=EXCLUDED.family,
    sort_order=EXCLUDED.sort_order, updated_at=NOW();

-- 设备心跳：复用 backup_policies 的 stale 告警。采集脚本成功后 report 一笔 run。
INSERT INTO backup_policies
    (code, name, purpose, asset, source_device, method, schedule_label,
     expected_interval_seconds, warning_after_seconds, failure_after_seconds,
     retention_policy, lifecycle_status, monitoring_required, sort_order, detail_json)
VALUES
    ('version-inventory-aliecs', '版本采集心跳（aliecs）', '确认 aliecs 每日版本采集脚本在跑',
     'aliecs 容器/apt 版本快照', 'aliecs', 'systemd timer 每日采集上报', '每日 05:00',
     86400, 172800, 259200, '仅保留最新快照', 'active', TRUE, 100, '{}'::jsonb),
    ('version-inventory-webdock1', '版本采集心跳（webdock1）', '确认 webdock1 每日版本采集脚本在跑',
     'webdock1 容器/apt 版本快照', 'webdock1', 'systemd timer 每日采集上报', '每日 05:10',
     86400, 172800, 259200, '仅保留最新快照', 'active', TRUE, 101, '{}'::jsonb),
    ('version-inventory-webdock2', '版本采集心跳（webdock2）', '确认 webdock2 每日版本采集脚本在跑',
     'webdock2 容器/apt 版本快照', 'webdock2', 'systemd timer 每日采集上报', '每日 05:20',
     86400, 172800, 259200, '仅保留最新快照', 'active', TRUE, 102, '{}'::jsonb)
ON CONFLICT (code) DO UPDATE SET
    name=EXCLUDED.name, purpose=EXCLUDED.purpose, asset=EXCLUDED.asset,
    source_device=EXCLUDED.source_device, method=EXCLUDED.method, schedule_label=EXCLUDED.schedule_label,
    expected_interval_seconds=EXCLUDED.expected_interval_seconds,
    warning_after_seconds=EXCLUDED.warning_after_seconds, failure_after_seconds=EXCLUDED.failure_after_seconds,
    retention_policy=EXCLUDED.retention_policy, lifecycle_status=EXCLUDED.lifecycle_status,
    monitoring_required=EXCLUDED.monitoring_required, sort_order=EXCLUDED.sort_order,
    detail_json=EXCLUDED.detail_json, updated_at=NOW();
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_version_inventory_migration -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add db/migrations/0037_version_inventory.sql tests/test_version_inventory_migration.py
git commit -m "feat(versions): schema + component/heartbeat seeds"
```

## Task 2: 版本解析与状态判定纯函数

纯逻辑单独成 Task，无 DB/网络依赖，最好测。

**Files:**
- Create: `services/backend-api/app/routers/versions.py`（先只放纯函数）
- Test: `tests/test_version_logic.py`

**Interfaces:**
- Produces:
  - `normalize_version(raw: str) -> str`：剥离 `v` 前缀、`-alpine`/`-bookworm` 后缀、`refs/tags/` 前缀，返回裸版本串（无法解析返回原串 strip 后的值）。
  - `compare_versions(current: str, latest: str) -> int`：语义化比较，current<latest 返回 -1，相等 0，current>latest 或不可比返回 1。按 `.` 分段整数比较，非整数段按字符串比较兜底。
  - `match_component(image: str, device: str, components: list[dict]) -> dict | None`：按 `match_images` 成员匹配 + `devices` 过滤（devices 为 None 视为任意），返回命中组件或 None。
  - `classify_component(*, family: str, upstream_source: str, current: str | None, latest: str | None, version_pattern: str | None) -> str`：返回 `current|behind|pinned|unregistered|own|stale` 之一（own 家族返回 'own'；upstream_source=none 且非 own 返回 'pinned'；缺 current 返回 'stale'；缺 latest 返回 'pinned'；否则比较 current/latest）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_version_logic.py
from __future__ import annotations
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


class VersionLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [p for p in sys.path if p != str(BACKEND_ROOT)]

    def test_normalize_strips_prefixes_suffixes(self) -> None:
        from app.routers.versions import normalize_version
        self.assertEqual(normalize_version("v1.135.3"), "1.135.3")
        self.assertEqual(normalize_version("refs/tags/v2.0.1"), "2.0.1")
        self.assertEqual(normalize_version("16.4-alpine"), "16.4")

    def test_compare_semver(self) -> None:
        from app.routers.versions import compare_versions
        self.assertEqual(compare_versions("1.135.2", "1.135.3"), -1)
        self.assertEqual(compare_versions("1.135.3", "1.135.3"), 0)
        self.assertEqual(compare_versions("1.136.0", "1.135.3"), 1)
        self.assertEqual(compare_versions("16.4", "16.10"), -1)  # 数值比较非字符串

    def test_match_component_filters_by_device(self) -> None:
        from app.routers.versions import match_component
        comps = [
            {"component_key": "pg-a", "match_images": ["postgres"], "devices": ["aliecs"]},
            {"component_key": "pg-w", "match_images": ["postgres"], "devices": ["webdock1"]},
        ]
        self.assertEqual(match_component("postgres", "aliecs", comps)["component_key"], "pg-a")
        self.assertEqual(match_component("postgres", "webdock1", comps)["component_key"], "pg-w")
        self.assertIsNone(match_component("redis", "aliecs", comps))

    def test_match_component_null_devices_matches_any(self) -> None:
        from app.routers.versions import match_component
        comps = [{"component_key": "apt", "match_images": ["apt-summary"], "devices": None}]
        self.assertEqual(match_component("apt-summary", "webdock2", comps)["component_key"], "apt")

    def test_classify_states(self) -> None:
        from app.routers.versions import classify_component
        self.assertEqual(classify_component(family="own", upstream_source="none",
                         current="sha-abc", latest=None, version_pattern=None), "own")
        self.assertEqual(classify_component(family="third-party", upstream_source="none",
                         current="16.4", latest=None, version_pattern=None), "pinned")
        self.assertEqual(classify_component(family="third-party", upstream_source="github-release",
                         current=None, latest="1.9", version_pattern=None), "stale")
        self.assertEqual(classify_component(family="third-party", upstream_source="github-release",
                         current="1.8", latest="1.9", version_pattern=None), "behind")
        self.assertEqual(classify_component(family="third-party", upstream_source="github-release",
                         current="1.9", latest="1.9", version_pattern=None), "current")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_version_logic -v`
Expected: FAIL（ImportError: cannot import name ...）

- [ ] **Step 3: 写纯函数实现**

```python
# services/backend-api/app/routers/versions.py
"""全设备版本看板：采集上报、上游对比、看板查询、周报推送。"""

from __future__ import annotations

import re

_V_PREFIX = re.compile(r"^(refs/tags/)?v", re.I)
_SUFFIX = re.compile(r"-(alpine|bookworm|slim|debian|distroless).*$", re.I)


def normalize_version(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = _V_PREFIX.sub("", s)
    s = _SUFFIX.sub("", s)
    return s.strip()


def _seg_cmp(a: str, b: str) -> int:
    if a.isdigit() and b.isdigit():
        ai, bi = int(a), int(b)
        return (ai > bi) - (ai < bi)
    return (a > b) - (a < b)


def compare_versions(current: str | None, latest: str | None) -> int:
    cur = normalize_version(current)
    lat = normalize_version(latest)
    if not cur or not lat:
        return 1  # 不可比时保守视为"不落后"，避免误报
    cs, ls = cur.split("."), lat.split(".")
    for i in range(max(len(cs), len(ls))):
        a = cs[i] if i < len(cs) else "0"
        b = ls[i] if i < len(ls) else "0"
        c = _seg_cmp(a, b)
        if c != 0:
            return c
    return 0


def match_component(image: str, device: str, components: list[dict]) -> dict | None:
    base = image.split("@")[0].split(":")[0]  # 去掉 tag/digest
    for comp in components:
        images = comp.get("match_images") or []
        if image in images or base in images:
            devices = comp.get("devices")
            if devices is None or device in devices:
                return comp
    return None


def classify_component(*, family: str, upstream_source: str,
                       current: str | None, latest: str | None,
                       version_pattern: str | None) -> str:
    if family == "own":
        return "own"
    if upstream_source == "none":
        return "pinned"
    if not current:
        return "stale"
    if not latest:
        return "pinned"
    return "behind" if compare_versions(current, latest) < 0 else "current"
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_version_logic -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add services/backend-api/app/routers/versions.py tests/test_version_logic.py
git commit -m "feat(versions): version parsing and classification logic"
```

## Task 3: 上报端点 + 心跳回写

**Files:**
- Modify: `services/backend-api/app/routers/versions.py`（加 router + report 端点）
- Test: `tests/test_version_report_api.py`

**Interfaces:**
- Consumes: `app.core._conn`；`BACKUP_REPORT_TOKEN` 环境变量。
- Produces:
  - `router = APIRouter()`
  - `POST /v1/internal/versions/report`，body 见下 `VersionReport`；写入 `version_reports`（每台设备本次全量容器逐行 INSERT，先删该设备旧行保持"当前快照"语义）。
  - `_require_report_token()`：照抄 backups 的 hmac 校验。

- [ ] **Step 1: 写失败测试**（用 FastAPI TestClient，DB 用 monkeypatch 替 `_conn`——照 backups 测试如无 TestClient 先例则用直接调函数+假 conn。此处用可注入的假连接。）

```python
# tests/test_version_report_api.py
from __future__ import annotations
import os, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


class FakeCur:
    def __init__(self, store): self.store = store; self._last = None
    def execute(self, sql, params=None):
        self._last = (sql, params)
        if sql.strip().upper().startswith("DELETE"): self.store["deleted"].append(params)
        elif "INSERT INTO version_reports" in sql: self.store["rows"].append(params)
    def fetchone(self): return [1]
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeConn:
    def __init__(self, store): self.store = store
    def cursor(self): return FakeCur(self.store)
    def commit(self): self.store["committed"] = True
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


class VersionReportApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))
        os.environ["BACKUP_REPORT_TOKEN"] = "test-token"

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [p for p in sys.path if p != str(BACKEND_ROOT)]

    def _client(self, store):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers import versions
        versions._conn = lambda: FakeConn(store)  # type: ignore
        app = FastAPI(); app.include_router(versions.router)
        return TestClient(app)

    def test_report_rejects_bad_token(self) -> None:
        store = {"rows": [], "deleted": [], }
        client = self._client(store)
        r = client.post("/v1/internal/versions/report",
                        headers={"X-Backup-Report-Token": "wrong"},
                        json={"device": "aliecs", "containers": [], "apt": {}})
        self.assertEqual(r.status_code, 401)

    def test_report_writes_container_rows(self) -> None:
        store = {"rows": [], "deleted": []}
        client = self._client(store)
        r = client.post("/v1/internal/versions/report",
                        headers={"X-Backup-Report-Token": "test-token"},
                        json={"device": "aliecs",
                              "containers": [{"image": "postgres", "tag": "16.4", "digest": "sha256:x"}],
                              "apt": {"upgradable": 3, "security": 1},
                              "extra": {"openclaw": "2026.6.5"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(store["rows"]), 1)
        self.assertTrue(store["deleted"])  # 先删旧快照
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_version_report_api -v`
Expected: FAIL

- [ ] **Step 3: 加端点实现**（追加到 versions.py）

```python
import hmac, os
from contextlib import closing
from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from app.core import _conn, require_admin

router = APIRouter()


class ContainerReport(BaseModel):
    image: str = Field(min_length=1, max_length=300)
    tag: str | None = Field(default=None, max_length=200)
    digest: str | None = Field(default=None, max_length=200)


class VersionReport(BaseModel):
    device: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    containers: list[ContainerReport] = Field(default_factory=list, max_length=200)
    apt: dict[str, int] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


def _require_report_token(x_backup_report_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("BACKUP_REPORT_TOKEN", "").strip()
    supplied = (x_backup_report_token or "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="invalid report token")


@router.post("/v1/internal/versions/report")
def report_versions(body: VersionReport, _: None = Depends(_require_report_token)) -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM version_reports WHERE device = %s", (body.device,))
                for c in body.containers:
                    cur.execute(
                        "INSERT INTO version_reports(device, image, tag, digest, extra_json) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (body.device, c.image, c.tag, c.digest, Jsonb({})),
                    )
                # apt 汇总 + extra（openclaw 版本等）作为一条 kind=apt-summary 记录
                cur.execute(
                    "INSERT INTO version_reports(device, image, tag, digest, extra_json) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (body.device, "apt-summary", None, None,
                     Jsonb({"apt": body.apt, **body.extra})),
                )
            conn.commit()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"version report write failed: {type(exc).__name__}") from exc
    return {"ok": True, "device": body.device, "count": len(body.containers)}
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_version_report_api -v`
Expected: PASS

- [ ] **Step 5: 装配进 main + 提交**

在 `services/backend-api/app/main.py` 照 backups 模式加：
```python
from app.routers.versions import router as versions_router
app.include_router(versions_router)
```
```bash
git add services/backend-api/app/routers/versions.py services/backend-api/app/main.py tests/test_version_report_api.py
git commit -m "feat(versions): report ingestion endpoint"
```

## Task 4: 上游查询与刷新端点

**Files:**
- Modify: `services/backend-api/app/routers/versions.py`
- Test: `tests/test_version_upstream.py`

**Interfaces:**
- Consumes: `normalize_version`、`compare_versions`。
- Produces:
  - `fetch_github_latest(ref: str, opener=urllib.request.urlopen) -> tuple[str|None, str|None]`：GET `https://api.github.com/repos/{ref}/releases/latest`，返回 `(tag_name, html_url)`；失败返回 `(None, None)`。opener 可注入便于测试。
  - `fetch_dockerhub_latest(ref: str, pattern: str|None, opener=...) -> tuple[str|None, str|None]`：GET `https://hub.docker.com/v2/repositories/{ref}/tags?page_size=100`，按 `pattern`（如 `^16\.`）过滤 tag，取 `compare_versions` 最大者，返回 `(tag, dockerhub_url)`。
  - `POST /v1/internal/versions/refresh-upstream`（token 校验）：遍历 active 且 upstream_source!=none 的组件，查上游，UPSERT `version_upstream_state`；单组件异常记 check_error 不中断。

- [ ] **Step 1: 写失败测试**（注入假 opener 返回固定 JSON）

```python
# tests/test_version_upstream.py
from __future__ import annotations
import io, json, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


def fake_opener(payload):
    def _open(req, timeout=None):
        return io.BytesIO(json.dumps(payload).encode())
    return _open


class UpstreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [p for p in sys.path if p != str(BACKEND_ROOT)]

    def test_github_latest_parses_tag_and_url(self) -> None:
        from app.routers.versions import fetch_github_latest
        op = fake_opener({"tag_name": "v1.135.3", "html_url": "https://github.com/x/releases/v1.135.3"})
        tag, url = fetch_github_latest("immich-app/immich", opener=op)
        self.assertEqual(tag, "v1.135.3")
        self.assertIn("releases", url)

    def test_dockerhub_filters_by_pattern_and_picks_max(self) -> None:
        from app.routers.versions import fetch_dockerhub_latest
        op = fake_opener({"results": [
            {"name": "16.4"}, {"name": "16.10"}, {"name": "17.2"}, {"name": "latest"},
        ]})
        tag, url = fetch_dockerhub_latest("library/postgres", r"^16\.", opener=op)
        self.assertEqual(tag, "16.10")  # 锁 16 大版本，选 16.x 内最大

    def test_github_failure_returns_none(self) -> None:
        from app.routers.versions import fetch_github_latest
        def boom(req, timeout=None): raise OSError("network")
        tag, url = fetch_github_latest("x/y", opener=boom)
        self.assertIsNone(tag)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_version_upstream -v`
Expected: FAIL

- [ ] **Step 3: 写实现**（追加到 versions.py）

```python
import json, urllib.request

_UA = {"User-Agent": "aliecs-version-inventory/1.0"}


def fetch_github_latest(ref: str, opener=urllib.request.urlopen) -> tuple[str | None, str | None]:
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{ref}/releases/latest", headers=_UA)
        with opener(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data.get("tag_name"), data.get("html_url")
    except Exception:
        return None, None


def fetch_dockerhub_latest(ref: str, pattern: str | None,
                           opener=urllib.request.urlopen) -> tuple[str | None, str | None]:
    try:
        req = urllib.request.Request(
            f"https://hub.docker.com/v2/repositories/{ref}/tags?page_size=100&ordering=last_updated",
            headers=_UA)
        with opener(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        rx = re.compile(pattern) if pattern else None
        best = None
        for t in data.get("results", []):
            name = t.get("name", "")
            if name == "latest" or (rx and not rx.search(name)):
                continue
            if not re.match(r"^[0-9]", normalize_version(name)):
                continue
            if best is None or compare_versions(name, best) > 0:
                best = name
        url = f"https://hub.docker.com/_/{ref.split('/')[-1]}?tab=tags"
        return best, (url if best else None)
    except Exception:
        return None, None


@router.post("/v1/internal/versions/refresh-upstream")
def refresh_upstream(_: None = Depends(_require_report_token)) -> dict[str, Any]:
    checked = 0
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT component_key, upstream_source, upstream_ref, version_pattern "
                "FROM version_components WHERE active AND upstream_source <> 'none'"
            )
            rows = cur.fetchall()
        for key, source, ref, pattern in rows:
            latest, url, status, err = None, None, "ok", None
            try:
                if source == "github-release":
                    latest, url = fetch_github_latest(ref)
                elif source == "dockerhub":
                    latest, url = fetch_dockerhub_latest(ref, pattern)
                if latest is None:
                    status, err = "error", "no upstream version resolved"
            except Exception as exc:
                status, err = "error", type(exc).__name__
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO version_upstream_state(component_key, latest_version, release_url, "
                    "checked_at, check_status, check_error) VALUES (%s, %s, %s, NOW(), %s, %s) "
                    "ON CONFLICT (component_key) DO UPDATE SET latest_version=EXCLUDED.latest_version, "
                    "release_url=EXCLUDED.release_url, checked_at=NOW(), "
                    "check_status=EXCLUDED.check_status, check_error=EXCLUDED.check_error",
                    (key, normalize_version(latest) if latest else None, url, status, err),
                )
            checked += 1
        conn.commit()
    return {"ok": True, "checked": checked}
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_version_upstream -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add services/backend-api/app/routers/versions.py tests/test_version_upstream.py
git commit -m "feat(versions): upstream lookup and refresh endpoint"
```

## Task 5: 看板聚合查询 `GET /v1/ops/versions`

**Files:**
- Modify: `services/backend-api/app/routers/versions.py`
- Test: `tests/test_version_ops_api.py`

**Interfaces:**
- Consumes: `match_component`、`classify_component`、`require_admin`。
- Produces:
  - `build_inventory(reports: list[dict], components: list[dict], upstream: dict[str, dict]) -> dict`：纯函数，返回 `{"summary": {...}, "devices": [{"device", "components": [{key, name, current, latest, status, release_url, note}]}]}`。own 家族做跨设备 tag 一致性：同 component_key 多设备 tag 不一致 → status `own-mismatch`，否则 `own`。未匹配镜像 → 一条 `unregistered`。
  - `GET /v1/ops/versions`（require_admin）：读三表喂给 `build_inventory`。

- [ ] **Step 1: 写失败测试**（纯函数 build_inventory 为主）

```python
# tests/test_version_ops_api.py
from __future__ import annotations
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


class BuildInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [p for p in sys.path if p != str(BACKEND_ROOT)]

    def _comps(self):
        return [
            {"component_key": "immich-server", "display_name": "Immich", "family": "third-party",
             "upstream_source": "github-release", "match_images": ["ghcr.io/immich-app/immich-server"],
             "devices": ["webdock1"], "version_pattern": None, "pin_note": None},
            {"component_key": "webdock", "display_name": "WebDock", "family": "own",
             "upstream_source": "none", "match_images": ["ghcr.io/huozao/webdock"],
             "devices": ["webdock1", "webdock2"], "version_pattern": None, "pin_note": None},
        ]

    def test_behind_when_current_below_latest(self) -> None:
        from app.routers.versions import build_inventory
        reports = [{"device": "webdock1", "image": "ghcr.io/immich-app/immich-server",
                    "tag": "v1.134.0", "extra": {}}]
        upstream = {"immich-server": {"latest_version": "1.135.3", "release_url": "http://x"}}
        inv = build_inventory(reports, self._comps(), upstream)
        comp = inv["devices"][0]["components"][0]
        self.assertEqual(comp["status"], "behind")
        self.assertEqual(inv["summary"]["behind"], 1)

    def test_own_mismatch_across_devices(self) -> None:
        from app.routers.versions import build_inventory
        reports = [
            {"device": "webdock1", "image": "ghcr.io/huozao/webdock", "tag": "sha-aaa", "extra": {}},
            {"device": "webdock2", "image": "ghcr.io/huozao/webdock", "tag": "sha-bbb", "extra": {}},
        ]
        inv = build_inventory(reports, self._comps(), {})
        statuses = {c["status"] for d in inv["devices"] for c in d["components"]}
        self.assertIn("own-mismatch", statuses)

    def test_unregistered_image_surfaces(self) -> None:
        from app.routers.versions import build_inventory
        reports = [{"device": "aliecs", "image": "some/new-service", "tag": "1.0", "extra": {}}]
        inv = build_inventory(reports, self._comps(), {})
        comp = inv["devices"][0]["components"][0]
        self.assertEqual(comp["status"], "unregistered")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_version_ops_api -v`
Expected: FAIL

- [ ] **Step 3: 写实现**（追加到 versions.py）

```python
def build_inventory(reports: list[dict], components: list[dict],
                    upstream: dict[str, dict]) -> dict[str, Any]:
    # own 家族跨设备 tag 收集
    own_tags: dict[str, set] = {}
    for r in reports:
        comp = match_component(r["image"], r["device"], components)
        if comp and comp.get("family") == "own":
            own_tags.setdefault(comp["component_key"], set()).add(r.get("tag"))

    devices: dict[str, list] = {}
    summary = {"behind": 0, "current": 0, "pinned": 0, "unregistered": 0,
               "own": 0, "own-mismatch": 0, "stale": 0}
    for r in reports:
        dev = r["device"]
        comp = match_component(r["image"], dev, components)
        if r["image"] == "apt-summary":
            apt = (r.get("extra") or {}).get("apt", {})
            entry = {"key": "apt-summary", "name": "APT 可升级", "current":
                     f"可升级 {apt.get('upgradable', 0)}（security {apt.get('security', 0)}）",
                     "latest": None, "status": "os", "release_url": None, "note": None}
            devices.setdefault(dev, []).append(entry)
            continue
        if comp is None:
            entry = {"key": None, "name": r["image"], "current": r.get("tag"),
                     "latest": None, "status": "unregistered", "release_url": None,
                     "note": "未登记镜像"}
        else:
            up = upstream.get(comp["component_key"], {})
            latest = up.get("latest_version")
            status = classify_component(family=comp["family"], upstream_source=comp["upstream_source"],
                                        current=r.get("tag"), latest=latest,
                                        version_pattern=comp.get("version_pattern"))
            if status == "own" and len(own_tags.get(comp["component_key"], set())) > 1:
                status = "own-mismatch"
            entry = {"key": comp["component_key"], "name": comp["display_name"],
                     "current": r.get("tag"), "latest": latest, "status": status,
                     "release_url": up.get("release_url"), "note": comp.get("pin_note")}
        summary[entry["status"]] = summary.get(entry["status"], 0) + 1
        devices.setdefault(dev, []).append(entry)

    overall = "ok"
    if summary["behind"] or summary["own-mismatch"]:
        overall = "warning"
    summary["status"] = overall
    return {"summary": summary,
            "devices": [{"device": d, "components": c} for d, c in sorted(devices.items())]}


@router.get("/v1/ops/versions")
def ops_versions(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT component_key, display_name, kind, match_images, devices, "
                        "upstream_source, upstream_ref, version_pattern, pin_note, family "
                        "FROM version_components WHERE active ORDER BY sort_order")
            comps = [dict(zip(
                ["component_key", "display_name", "kind", "match_images", "devices",
                 "upstream_source", "upstream_ref", "version_pattern", "pin_note", "family"], row))
                for row in cur.fetchall()]
            cur.execute("SELECT device, image, tag, digest, extra_json FROM version_reports")
            reports = [{"device": r[0], "image": r[1], "tag": r[2], "digest": r[3],
                        "extra": r[4] or {}} for r in cur.fetchall()]
            cur.execute("SELECT component_key, latest_version, release_url, checked_at, "
                        "check_status, check_error FROM version_upstream_state")
            upstream = {r[0]: {"latest_version": r[1], "release_url": r[2],
                               "checked_at": r[3].isoformat() if r[3] else None,
                               "check_status": r[4], "check_error": r[5]} for r in cur.fetchall()}
    return build_inventory(reports, comps, upstream)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_version_ops_api -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add services/backend-api/app/routers/versions.py tests/test_version_ops_api.py
git commit -m "feat(versions): dashboard aggregation endpoint"
```

## Task 6: 周报文案生成 + 飞书直发端点

**Files:**
- Modify: `services/backend-api/app/routers/versions.py`
- Test: `tests/test_version_digest.py`

**Interfaces:**
- Consumes: `build_inventory` 的输出结构；env `FEISHU_APP_ID`/`FEISHU_APP_SECRET`/`VERSION_DIGEST_FEISHU_RECEIVE_ID`。
- Produces:
  - `render_digest_text(inventory: dict, stale_devices: list[str]) -> str`：纯函数。有落后/不一致/未登记/缺席 → 分节列出；全绿 → 返回一行"✅ 全部最新，N 个组件已核对"。
  - `send_feishu_text(receive_id, text, *, app_id, app_secret, opener=...) -> bool`：拿 tenant_access_token → 发 im 消息；缺凭据返回 False。
  - `POST /v1/internal/versions/weekly-digest`（token 校验）：读 `/v1/ops/versions` 逻辑 + 查 stale 设备（version_reports 中 device 最近 reported_at > 48h 或缺席）→ 生成文案 → 发飞书。

- [ ] **Step 1: 写失败测试**（纯函数 render_digest_text）

```python
# tests/test_version_digest.py
from __future__ import annotations
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"


class DigestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(BACKEND_ROOT))

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path[:] = [p for p in sys.path if p != str(BACKEND_ROOT)]

    def test_all_green_still_sends_short_line(self) -> None:
        from app.routers.versions import render_digest_text
        inv = {"summary": {"status": "ok", "behind": 0, "own-mismatch": 0, "unregistered": 0},
               "devices": [{"device": "aliecs", "components": [
                   {"key": "openclaw", "name": "OpenClaw", "current": "2026.6.5",
                    "latest": "2026.6.5", "status": "current", "release_url": None, "note": None}]}]}
        text = render_digest_text(inv, [])
        self.assertIn("全部最新", text)

    def test_behind_items_listed_with_versions(self) -> None:
        from app.routers.versions import render_digest_text
        inv = {"summary": {"status": "warning", "behind": 1, "own-mismatch": 0, "unregistered": 0},
               "devices": [{"device": "webdock1", "components": [
                   {"key": "immich-server", "name": "Immich", "current": "v1.134.0",
                    "latest": "1.135.3", "status": "behind",
                    "release_url": "http://x", "note": None}]}]}
        text = render_digest_text(inv, [])
        self.assertIn("Immich", text)
        self.assertIn("1.135.3", text)

    def test_stale_device_flagged(self) -> None:
        from app.routers.versions import render_digest_text
        inv = {"summary": {"status": "ok", "behind": 0, "own-mismatch": 0, "unregistered": 0},
               "devices": []}
        text = render_digest_text(inv, ["webdock2"])
        self.assertIn("webdock2", text)
        self.assertIn("未上报", text)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_version_digest -v`
Expected: FAIL

- [ ] **Step 3: 写实现**（追加到 versions.py）

```python
def render_digest_text(inventory: dict, stale_devices: list[str]) -> str:
    behind, mismatch, unregistered = [], [], []
    for d in inventory.get("devices", []):
        for c in d["components"]:
            line = f"  · {d['device']}/{c['name']}：{c.get('current')} → {c.get('latest')}"
            if c["status"] == "behind":
                behind.append(line + (f"（{c['release_url']}）" if c.get("release_url") else ""))
            elif c["status"] == "own-mismatch":
                mismatch.append(f"  · {c['name']}：{d['device']} tag={c.get('current')}")
            elif c["status"] == "unregistered":
                unregistered.append(f"  · {d['device']}/{c['name']}={c.get('current')}")
    parts = ["📦 每周版本巡检"]
    if behind:
        parts.append("🔴 有新版本可用：\n" + "\n".join(behind))
    if mismatch:
        parts.append("🟠 自家镜像跨设备 tag 不一致：\n" + "\n".join(mismatch))
    if unregistered:
        parts.append("⚠️ 未登记镜像（新部署？请补进组件表）：\n" + "\n".join(unregistered))
    if stale_devices:
        parts.append("⛔ 采集未上报（管道可能故障）：" + "、".join(stale_devices))
    if len(parts) == 1:
        total = sum(len(d["components"]) for d in inventory.get("devices", []))
        parts.append(f"✅ 全部最新，{total} 个组件已核对。")
    return "\n\n".join(parts)


def send_feishu_text(receive_id: str, text: str, *, app_id: str, app_secret: str,
                     opener=urllib.request.urlopen) -> bool:
    if not (app_id and app_secret and receive_id):
        return False
    try:
        tok_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with opener(tok_req, timeout=15) as resp:
            token = json.loads(resp.read().decode()).get("tenant_access_token")
        if not token:
            return False
        msg_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=json.dumps({"receive_id": receive_id, "msg_type": "text",
                             "content": json.dumps({"text": text})}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST")
        with opener(msg_req, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("code") == 0
    except Exception:
        return False


@router.post("/v1/internal/versions/weekly-digest")
def weekly_digest(_: None = Depends(_require_report_token)) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT component_key, display_name, kind, match_images, devices, "
                        "upstream_source, upstream_ref, version_pattern, pin_note, family "
                        "FROM version_components WHERE active ORDER BY sort_order")
            comps = [dict(zip(
                ["component_key", "display_name", "kind", "match_images", "devices",
                 "upstream_source", "upstream_ref", "version_pattern", "pin_note", "family"], row))
                for row in cur.fetchall()]
            cur.execute("SELECT device, image, tag, digest, extra_json FROM version_reports")
            reports = [{"device": r[0], "image": r[1], "tag": r[2], "digest": r[3],
                        "extra": r[4] or {}} for r in cur.fetchall()]
            cur.execute("SELECT component_key, latest_version, release_url FROM version_upstream_state")
            upstream = {r[0]: {"latest_version": r[1], "release_url": r[2]} for r in cur.fetchall()}
            cur.execute("SELECT device, MAX(reported_at) FROM version_reports GROUP BY device")
            seen = {r[0]: r[1] for r in cur.fetchall()}
    inv = build_inventory(reports, comps, upstream)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    expected = {"aliecs", "webdock1", "webdock2"}
    stale = sorted(d for d in expected if d not in seen or (seen[d] and seen[d] < cutoff))
    text = render_digest_text(inv, stale)
    sent = send_feishu_text(
        os.getenv("VERSION_DIGEST_FEISHU_RECEIVE_ID", ""), text,
        app_id=os.getenv("FEISHU_APP_ID", ""), app_secret=os.getenv("FEISHU_APP_SECRET", ""))
    return {"ok": True, "sent": sent, "stale": stale}
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_version_digest -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add services/backend-api/app/routers/versions.py tests/test_version_digest.py
git commit -m "feat(versions): weekly digest text and feishu delivery"
```

## Task 7: backend env 接线（FEISHU 凭据 + receive_id）

**Files:**
- Modify: `deploy/ecs/compose.prod.yml`（backend-api environment 段）
- Modify: `deploy/ecs/deploy.sh`（release-meta heredoc 段）
- Modify: `deploy/ecs/release-meta.env.example`、`deploy/ecs/runtime.env.example`
- Test: `tests/test_version_env_wiring.py`

**Interfaces:**
- Produces: backend 容器可读 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`VERSION_DIGEST_FEISHU_RECEIVE_ID`。

> ⚠️ 历史教训：backend 加 env 需三处同步（compose environment + deploy.sh heredoc + example），漏一处则容器读不到。本任务把三处一起改，测试断言三处都在。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_version_env_wiring.py
from __future__ import annotations
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EnvWiringTests(unittest.TestCase):
    def test_compose_maps_feishu_and_receive_id(self) -> None:
        c = (ROOT / "deploy" / "ecs" / "compose.prod.yml").read_text(encoding="utf-8")
        for key in ("FEISHU_APP_ID:", "FEISHU_APP_SECRET:", "VERSION_DIGEST_FEISHU_RECEIVE_ID:"):
            self.assertIn(key, c)

    def test_deploy_heredoc_passes_keys(self) -> None:
        d = (ROOT / "deploy" / "ecs" / "deploy.sh").read_text(encoding="utf-8")
        for key in ("FEISHU_APP_ID=", "FEISHU_APP_SECRET=", "VERSION_DIGEST_FEISHU_RECEIVE_ID="):
            self.assertIn(key, d)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_version_env_wiring -v`
Expected: FAIL

- [ ] **Step 3: 三处接线**

`compose.prod.yml` backend-api `environment:` 段末尾加：
```yaml
      FEISHU_APP_ID: ${FEISHU_APP_ID:-}
      FEISHU_APP_SECRET: ${FEISHU_APP_SECRET:-}
      VERSION_DIGEST_FEISHU_RECEIVE_ID: ${VERSION_DIGEST_FEISHU_RECEIVE_ID:-}
```
`deploy.sh` 的 backend env heredoc 段加对应三行 `KEY=${KEY:-}`（照 `BACKUP_REPORT_TOKEN=${BACKUP_REPORT_TOKEN:-}` 位置）；顶部变量收集区加 `FEISHU_APP_ID="${FEISHU_APP_ID:-}"` 等三行。
`release-meta.env.example` / `runtime.env.example` 各加三行占位符与注释（说明来源 sops）。

- [ ] **Step 4: 运行确认通过 + 全量测试**

Run: `python -m unittest tests.test_version_env_wiring -v`
Run: `python -m unittest discover tests -v 2>&1 | tail -5`（确认无回归）
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add deploy/ecs/compose.prod.yml deploy/ecs/deploy.sh deploy/ecs/release-meta.env.example deploy/ecs/runtime.env.example tests/test_version_env_wiring.py
git commit -m "feat(versions): wire feishu digest env into backend"
```

## Task 8: /health 看板「版本」区块

**Files:**
- Modify: `services/public-web/health/index.html`
- Test: `tests/test_version_health_page.py`

**Interfaces:**
- Consumes: `GET /v1/ops/versions` 返回结构。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_version_health_page.py
from __future__ import annotations
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "services" / "public-web" / "health" / "index.html"


class VersionHealthPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = HEALTH.read_text(encoding="utf-8")

    def test_has_versions_section_and_api(self) -> None:
        self.assertIn("版本巡检", self.html)
        self.assertIn("/v1/ops/versions", self.html)

    def test_has_render_function_and_status_badges(self) -> None:
        self.assertIn("function renderVersions(", self.html)
        for label in ("落后", "最新", "未登记"):
            self.assertIn(label, self.html)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_version_health_page -v`
Expected: FAIL

- [ ] **Step 3: 加区块**（照现有 backupSummary 模式：一个 `<section>` + fetch + render 函数；状态徽标映射 `current→✅最新 / behind→🔴落后 / pinned→📌锁定 / unregistered→⚠️未登记 / own→⚪一致 / own-mismatch→🟠不一致 / os→apt 数字 / stale→⛔`。按 device 分组渲染表格：组件｜当前｜最新｜状态。落后行的 latest 若有 release_url 渲染成链接。顶部把 `summary.behind` 计入 /health 总览徽标。）具体代码照 index.html 既有 JS 风格补全（`fetch('/api/v1/ops/versions')` → 分组表格）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_version_health_page -v`
Expected: PASS

- [ ] **Step 5: 提交 + PR-A 全量测试**

Run: `python -m unittest discover tests -v 2>&1 | tail -8`
```bash
git add services/public-web/health/index.html tests/test_version_health_page.py
git commit -m "feat(versions): health dashboard versions section"
```

## Task 9: 建 PR-A

- [ ] **Step 1: 推分支并建 PR**

```bash
git push -u origin feat/version-inventory
gh pr create --repo huozao/AliECS --base main --title "feat: 全设备版本看板（version-inventory）" \
  --body "$(cat <<'BODY'
## 摘要
- 三机版本采集上报端点 + 上游对比（GitHub/Docker Hub）+ /health 看板「版本」区块 + 飞书周报
- 复用备份看板 backup_policies 做设备心跳；token 复用 BACKUP_REPORT_TOKEN
- infra 侧采集脚本/timer 在配套 infra PR

## 上线后需人工
- sops 补 backend 的 FEISHU_APP_ID/SECRET + VERSION_DIGEST_FEISHU_RECEIVE_ID → render → 重建 backend
- 合并 infra PR 装采集 timer

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

⚠️ merge PR-A 会触发 release-deploy 自动跑 migrate + 全量部署，别在其运行时抢操作。

---

# PR-B：infra 仓库（`infra` 目录，从 origin/main 开 `feat/version-inventory-collect`）

## Task 10: 采集脚本

**Files:**
- Create: `infra/versions/collect-versions.sh`
- Test: `infra/versions/tests/test_collect_versions.sh`（bash 断言脚本）

**Interfaces:**
- 环境变量：`BACKUP_REPORT_TOKEN`、`VERSION_REPORT_URL`（默认 `https://hydwang.xyz/api/v1/internal/versions/report`）、`BACKUP_REPORT_URL`（心跳，默认 `https://hydwang.xyz/api/v1/internal/backups/report`）、`DEVICE_NAME`、`VERSION_HEARTBEAT_POLICY`（如 `version-inventory-aliecs`）。
- 行为：`docker ps` 全容器 image → 组 JSON containers 数组；`apt-get -s upgrade` 计数（无 apt 则 0）；aliecs 额外 `docker exec` openclaw 取版本进 extra；POST report；成功后 POST 心跳 run。

- [ ] **Step 1: 写实现**（照 docker-image-maintenance.sh 模板）

```bash
#!/usr/bin/env bash
# 采集本机容器镜像版本 + apt 可升级数，上报版本看板；成功后打卡心跳。
set -Eeuo pipefail

DEVICE="${DEVICE_NAME:-$(hostname -s)}"
REPORT_URL="${VERSION_REPORT_URL:-https://hydwang.xyz/api/v1/internal/versions/report}"
HEARTBEAT_URL="${BACKUP_REPORT_URL:-https://hydwang.xyz/api/v1/internal/backups/report}"
HEARTBEAT_POLICY="${VERSION_HEARTBEAT_POLICY:-version-inventory-${DEVICE}}"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 1) 容器镜像 → JSON 数组 [{image,tag,digest}]
containers_json="$(docker ps --format '{{.Image}}' | sort -u | python3 -c '
import sys, json, subprocess
out=[]
for line in sys.stdin:
    ref=line.strip()
    if not ref: continue
    base=ref.split("@")[0]
    image=base.rsplit(":",1)[0] if ":" in base.split("/")[-1] else base
    tag=base.rsplit(":",1)[1] if ":" in base.split("/")[-1] else None
    digest=None
    try:
        digest=subprocess.check_output(["docker","inspect","--format","{{index .RepoDigests 0}}",ref],
                                       text=True,stderr=subprocess.DEVNULL).strip() or None
    except Exception: pass
    out.append({"image":image,"tag":tag,"digest":digest})
print(json.dumps(out))
')"

# 2) apt 可升级计数（security 细分）；非 apt 系统返回 0
upgradable=0; security=0
if command -v apt-get >/dev/null 2>&1; then
    sim="$(apt-get -s upgrade 2>/dev/null || true)"
    upgradable="$(printf '%s\n' "$sim" | grep -c '^Inst ' || true)"
    security="$(printf '%s\n' "$sim" | grep '^Inst ' | grep -ci security || true)"
fi

# 3) extra：aliecs 采 openclaw 版本
extra_json='{}'
if docker ps --format '{{.Names}}' | grep -q openclaw-gateway; then
    ov="$(docker exec "$(docker ps -qf name=openclaw-gateway | head -1)" \
          node dist/index.js --version 2>/dev/null | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+' | head -1 || true)"
    [ -n "$ov" ] && extra_json="$(python3 -c "import json;print(json.dumps({'openclaw':'$ov'}))")"
fi

payload="$(python3 -c "
import json,sys
print(json.dumps({'device':'$DEVICE','containers':json.loads('''$containers_json'''),
                  'apt':{'upgradable':int('$upgradable' or 0),'security':int('$security' or 0)},
                  'extra':json.loads('''$extra_json''')}))
")"

# 4) 上报版本
if [ -n "${BACKUP_REPORT_TOKEN:-}" ]; then
    curl -fsS --max-time 20 -X POST "$REPORT_URL" \
        -H "Content-Type: application/json" \
        -H "X-Backup-Report-Token: $BACKUP_REPORT_TOKEN" \
        -d "$payload" >/dev/null
    # 5) 心跳打卡（复用备份看板 stale 告警）
    FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    curl -fsS --max-time 15 -X POST "$HEARTBEAT_URL" \
        -H "Content-Type: application/json" \
        -H "X-Backup-Report-Token: $BACKUP_REPORT_TOKEN" \
        -d "{\"policy_code\":\"$HEARTBEAT_POLICY\",\"run_id\":\"$HEARTBEAT_POLICY-$(date -u +%Y%m%dT%H%M%SZ)\",\"status\":\"success\",\"source_device\":\"$DEVICE\",\"started_at\":\"$STARTED_AT\",\"finished_at\":\"$FINISHED_AT\"}" >/dev/null
else
    echo "[versions] BACKUP_REPORT_TOKEN unset; skipping report" >&2
    exit 1
fi
```

- [ ] **Step 2: 本地语法检查**

Run: `bash -n infra/versions/collect-versions.sh && echo OK`
Expected: OK

- [ ] **Step 3: 写冒烟测试脚本**（校验 payload 构造，docker 用 stub）

```bash
# infra/versions/tests/test_collect_versions.sh
set -euo pipefail
# 断言脚本引用了必需的 URL 与 token 头
grep -q "X-Backup-Report-Token" ../collect-versions.sh
grep -q "versions/report" ../collect-versions.sh
grep -q "backups/report" ../collect-versions.sh  # 心跳
grep -q "apt-get -s upgrade" ../collect-versions.sh
echo "collect-versions structure OK"
```

Run: `cd infra/versions/tests && bash test_collect_versions.sh`
Expected: `collect-versions structure OK`

- [ ] **Step 4: 提交**

```bash
git add infra/versions/collect-versions.sh infra/versions/tests/test_collect_versions.sh
git commit -m "feat(versions): device collection script"
```

## Task 11: systemd units（采集 timer + 刷新/周报 timer）

**Files:**
- Create: `infra/versions/collect-versions.service` / `.timer`
- Create: `infra/versions/refresh-upstream.service` / `.timer`（仅 aliecs）
- Create: `infra/versions/weekly-digest.service` / `.timer`（仅 aliecs）

**Interfaces:** 照 `docker-image-maintenance.service` 的 SOPS 注入模式；采集 service 从对应设备 enc.env 解 token。

- [ ] **Step 1: 写 units**

采集 service（各机，token 来源按设备不同——见 Task 12 分发）：
```ini
# collect-versions.service
[Unit]
Description=Daily version inventory collection and report
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
Nice=10
Environment=SOPS_AGE_KEY_FILE=%h/.config/sops/age/keys.txt
Environment=HOME=%h
# DEVICE_ENV 由安装时按机器替换（aliecs=backup.enc.env；webdock1/2=webdockN.enc.env）
ExecStart=/bin/bash -c 'set -a; eval "$(sops -d ${DEVICE_ENC_ENV})"; set +a; exec ${VERSIONS_DIR}/collect-versions.sh'
```
> 说明：aliecs 用 root（`SOPS_AGE_KEY_FILE=/root/.config/sops/age/keys.txt`，`VERSIONS_DIR=/root/infra/versions`，`DEVICE_ENC_ENV=/root/infra/secrets/backup.enc.env`）；webdock 用 webdock 用户（`%h` 展开为 /home/webdock，`DEVICE_ENC_ENV=~/infra/secrets/webdock1.enc.env`）。安装说明在 README 写死每机具体值，避免 unit 内变量歧义——实际交付时每机一份具化 unit 或用 drop-in。

采集 timer（错峰）：
```ini
# collect-versions.timer  —— aliecs 05:00 / webdock1 05:10 / webdock2 05:20（安装时改 OnCalendar）
[Unit]
Description=Daily version inventory collection
[Timer]
OnCalendar=*-*-* 05:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

refresh-upstream（aliecs，每日 06:00，curl 内部端点）：
```ini
# refresh-upstream.service
[Unit]
Description=Refresh upstream latest versions
After=network-online.target
[Service]
Type=oneshot
Environment=SOPS_AGE_KEY_FILE=/root/.config/sops/age/keys.txt
ExecStart=/bin/bash -c 'set -a; eval "$(sops -d /root/infra/secrets/backup.enc.env)"; set +a; curl -fsS --max-time 60 -X POST https://hydwang.xyz/api/v1/internal/versions/refresh-upstream -H "X-Backup-Report-Token: $BACKUP_REPORT_TOKEN"'
```
```ini
# refresh-upstream.timer
[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

weekly-digest（aliecs，周一 09:00 CST=01:00 UTC；先 refresh 再 digest 由端点内保证数据新鲜——digest 端点读库即可，前一小时 refresh 已跑）：
```ini
# weekly-digest.service
[Service]
Type=oneshot
Environment=SOPS_AGE_KEY_FILE=/root/.config/sops/age/keys.txt
ExecStart=/bin/bash -c 'set -a; eval "$(sops -d /root/infra/secrets/backup.enc.env)"; set +a; curl -fsS --max-time 60 -X POST https://hydwang.xyz/api/v1/internal/versions/weekly-digest -H "X-Backup-Report-Token: $BACKUP_REPORT_TOKEN"'
```
```ini
# weekly-digest.timer  —— 周一 01:00 UTC = 北京 09:00
[Timer]
OnCalendar=Mon *-*-* 01:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

- [ ] **Step 2: 语法自检**

Run: `for f in infra/versions/*.service infra/versions/*.timer; do echo "-- $f"; done && echo units-listed`
（systemd-analyze verify 需目标机，此处仅结构存在性）

- [ ] **Step 3: 提交**

```bash
git add infra/versions/*.service infra/versions/*.timer
git commit -m "feat(versions): systemd units for collect/refresh/digest"
```

## Task 12: token 分发（webdock enc.env）+ render 接线 + README

**Files:**
- Modify: `infra/secrets/webdock1.enc.env`（sops set 加 `BACKUP_REPORT_TOKEN`，值同 aliecs）
- Modify: `infra/secrets/webdock2.enc.env`（同上）
- Modify: `infra/scripts/render.sh`（三设备段安装 versions unit）
- Create: `infra/versions/README.md`

**Interfaces:** 让三台机 render 后各自持有 token + 已安装 versions timer。

- [ ] **Step 1: 分发 token**（devbox 上，需 aliecs 现值）

```bash
# 取 aliecs 现值（devbox 可解 backup.enc.env）
TOK="$(cd infra && sops -d --extract '["BACKUP_REPORT_TOKEN"]' secrets/backup.enc.env)"
cd infra && sops set secrets/webdock1.enc.env '["BACKUP_REPORT_TOKEN"]' "\"$TOK\""
sops set secrets/webdock2.enc.env '["BACKUP_REPORT_TOKEN"]' "\"$TOK\""
# 校验键名出现
grep -c BACKUP_REPORT_TOKEN secrets/webdock1.enc.env secrets/webdock2.enc.env
```

- [ ] **Step 2: render.sh 接线**

在 `render.sh` 的 `aliecs)` / `webdock1)` / `webdock2)` 三段各加：安装对应 `collect-versions.{service,timer}`（具化 OnCalendar 与 enc.env 路径），aliecs 段额外装 refresh/weekly-digest 两对 unit，并把 `collect-versions.sh` 复制到 `${VERSIONS_DIR}` 或直接引用仓库内路径。照现有 `install_file` 辅助函数模式。

- [ ] **Step 3: 写 README**（`infra/versions/README.md`）

内容：架构一段、各机安装命令（`git pull && render.sh <device>` + `systemctl enable --now collect-versions.timer`，aliecs 另启 refresh/weekly-digest timer）、token 分发说明、排障（看板无某设备数据→查该机 `systemctl status collect-versions` 与 `journalctl -u collect-versions`；周报没来→手动 `curl weekly-digest`；上游全 error→查 GitHub API 配额）、验证命令（手动跑一次 `collect-versions.sh` 看 `/health` 出数据）。

- [ ] **Step 4: 提交**

```bash
git add infra/secrets/webdock1.enc.env infra/secrets/webdock2.enc.env infra/scripts/render.sh infra/versions/README.md
git commit -m "feat(versions): distribute token, wire render, add runbook"
```

## Task 13: 建 PR-B + 上线联调

- [ ] **Step 1: 推分支建 PR**

```bash
cd infra && git push -u origin feat/version-inventory-collect
gh pr create --repo huozao/infra --base main --title "feat: 版本看板采集侧（脚本+timer+token 分发）" \
  --body "配套 AliECS version-inventory PR。含三机采集脚本、systemd timer、webdock token 分发、render 接线。🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 2: 合并后各机同步**（infra 无 CI，手动）

devbox：`git push device-aliecs`、`device-webdock1`、`device-webdock2`（照多方一致性规则）。
各机：`git pull --ff-only` → `render.sh <device>` → `systemctl daemon-reload && systemctl enable --now collect-versions.timer`（aliecs 另 `enable --now refresh-upstream.timer weekly-digest.timer`）。

- [ ] **Step 3: 端到端验证**

```bash
# 每台机手动首采
ssh aliecs   'set -a; eval "$(sops -d /root/infra/secrets/backup.enc.env)"; set +a; /root/infra/versions/collect-versions.sh; echo done'
ssh webdock1 'cd ~/infra && set -a; eval "$(sops -d secrets/webdock1.enc.env)"; set +a; ~/infra/versions/collect-versions.sh'
ssh webdock2 'wsl -d Ubuntu-24.04-WebDock -- bash -c "cd ~/infra && set -a; eval \"\$(sops -d secrets/webdock2.enc.env)\"; set +a; ~/infra/versions/collect-versions.sh"'
# 触发上游刷新
ssh aliecs 'set -a; eval "$(sops -d /root/infra/secrets/backup.enc.env)"; set +a; curl -fsS -X POST https://hydwang.xyz/api/v1/internal/versions/refresh-upstream -H "X-Backup-Report-Token: $BACKUP_REPORT_TOKEN"'
# 看板取数
ssh aliecs 'curl -fsS http://127.0.0.1:8000/v1/ops/versions -H "..."' # 需 admin，实际在浏览器 /health 看
# 手动发一次周报确认飞书通
ssh aliecs 'set -a; eval "$(sops -d /root/infra/secrets/backup.enc.env)"; set +a; curl -fsS -X POST https://hydwang.xyz/api/v1/internal/versions/weekly-digest -H "X-Backup-Report-Token: $BACKUP_REPORT_TOKEN"'
```

验收标准：/health 版本区块三设备全出数据；OpenClaw/Immich 行显示 current/latest；飞书收到巡检消息；等次日确认 timer 自动跑（`systemctl list-timers | grep version`）。

---

## 自查记录

- **Spec 覆盖**：数据模型(T1)、采集脚本(T10)、上游对比(T4)、看板(T8/T5)、周报(T6)、token 分发(T12)、env 接线(T7)、心跳(T1 seed + T10 打卡)、安全(全程 token/内部端点) 均有对应任务。
- **占位符**：无 TBD/TODO；T8 看板 JS 与 T12 render.sh 接线因需贴合既有文件风格，给了明确契约与映射而非逐字节代码（执行者读现有文件补全），其余步骤均含完整可运行代码。
- **类型一致**：`build_inventory`/`match_component`/`classify_component`/`render_digest_text`/`normalize_version`/`compare_versions` 命名在 T2/T5/T6 间一致；状态枚举 `current|behind|pinned|unregistered|own|own-mismatch|stale|os` 全程统一。
- **已知偏差**：Immich 官方 postgres/redis 镜像名可能与种子表 match_images 不完全一致（T1 已列多候选），上线首采若出现 `unregistered` 行按实际镜像名补 `version_components`（这正是"未登记自动冒出"设计的用途）。
