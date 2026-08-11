# 统一同步平台中心 P0 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立统一同步平台的 4 张元数据表，并把 `/exports/` 与 `/tplus-sync/` 两页逐字重复的 CSS 与鉴权 JS 抽成共享资产，为后续 P1–P5 打地基。

**Architecture:** 纯地基阶段，**零行为变化**。数据库侧只加表不改表、不写数据、无代码读写它们；前端侧只把两份逐字相同的资产合成一份，两页渲染与交互结果必须完全不变。

**Tech Stack:** PostgreSQL 迁移（`db/migrations/*.sql`，由 `deploy/ecs/migrate.sh` 按文件名排序执行并登记 `schema_migrations`）、原生 HTML/JS（无构建步骤，`services/public-web` 由 nginx 直接托管）、Python `unittest`（测试为读文件的文本断言，不依赖数据库或浏览器）。

**设计依据：** `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md` 第 4 节（数据模型）与 7.4 节（前端资产清理）。

## Global Constraints

- 表名前缀统一 `sync_job_*`，**不得**与现有 `sync_runs` / `integration_sync_runs` 撞名。
- 迁移必须可重复执行：一律 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`。
- 不修改历史迁移文件；新迁移编号 `0048`（当前最大为 `0047_tplus_inventory_records.sql`）。
- 本阶段**不写入**任何业务数据，**不 seed** `sync_jobs`（动态作业 `wecom.doc.<source_id>` 由 P1 的 worker upsert 登记）。
- 不碰 `tplus_bom_records`、`external_records`、`tplus_inventory_records`。
- 不碰 `/formula/` 的任何读取路径，不碰 `RECIPE_*` / `TPLUS_EXPORT_DIR` 环境变量。
- 不动 `services/public-web/health/index.html`（它的 CSS 与另两页**不同**，md5 已核对不一致）。
- 本地验证只用 `local/docker-compose.local.yml` + `local/.env.local`，不读生产 env、不连生产库。
- AliECS 走分支 + PR，不直推 main。所有写 `.git` 的命令串行执行。

---

### Task 1: 迁移 0048 — 建 4 张元数据表

**Files:**
- Create: `db/migrations/0048_sync_job_platform.sql`
- Test: `tests/test_migration_sync_job_platform.py`

**Interfaces:**
- Consumes: 无（本阶段第一个任务）
- Produces: 四张表 `sync_jobs`、`sync_job_runs`、`sync_job_steps`、`sync_job_alerts`。P1 的 worker 双写将 INSERT `sync_job_runs`(job_id, trigger, status, started_at) 与 `sync_job_steps`(run_id, seq, name, status)；P3 的 notifier 依赖 `sync_job_alerts` 上的 partial unique index 做抢占去重。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_migration_sync_job_platform.py`：

```python
from __future__ import annotations

import unittest
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "migrations"
    / "0048_sync_job_platform.sql"
)


class SyncJobPlatformMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = MIGRATION.read_text(encoding="utf-8")

    def test_creates_four_tables(self) -> None:
        for table in ("sync_jobs", "sync_job_runs", "sync_job_steps", "sync_job_alerts"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", self.sql)

    def test_is_rerunnable(self) -> None:
        # 迁移按文件名排序全量扫过，必须可重复执行。
        self.assertNotIn("CREATE TABLE sync_", self.sql)
        self.assertNotIn("CREATE INDEX idx_sync_job", self.sql)

    def test_job_key_is_unique(self) -> None:
        self.assertIn("job_key TEXT NOT NULL UNIQUE", self.sql)

    def test_open_alert_is_deduped_by_partial_unique_index(self) -> None:
        # P3 notifier 的抢占去重完全依赖这条索引：一个作业一种告警同时只能有一条 open。
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_job_alerts_open", self.sql)
        self.assertIn("WHERE state = 'open'", self.sql)

    def test_runs_carry_error_kind_and_legacy_ref(self) -> None:
        # error_kind 是页面与告警的分类依据；legacy_ref 是双写期回指旧表的追溯键。
        self.assertIn("error_kind TEXT", self.sql)
        self.assertIn("legacy_ref JSONB", self.sql)

    def test_jobs_carry_freshness_and_artifact_fields(self) -> None:
        # 新鲜度与产出物新鲜度是本项目相对旧页面的核心增量，不能漏建。
        self.assertIn("freshness_sla_seconds INTEGER", self.sql)
        self.assertIn("artifact_glob TEXT", self.sql)

    def test_steps_reference_runs_with_cascade(self) -> None:
        self.assertIn("REFERENCES sync_job_runs(id) ON DELETE CASCADE", self.sql)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m unittest tests.test_migration_sync_job_platform -v`
Expected: FAIL —— `FileNotFoundError`，因为迁移文件还不存在。

- [ ] **Step 3: 写迁移**

创建 `db/migrations/0048_sync_job_platform.sql`：

```sql
-- 统一同步平台元数据层（设计：docs/superpowers/specs/2026-08-11-unified-sync-center-design.md）
-- 只统一「元数据」：作业登记、运行、步骤、告警。
-- 业务数据仍归各自的表（external_records / tplus_bom_records / tplus_inventory_records），
-- 本迁移不碰它们，也不写入任何数据。
--
-- 与现有两套 run 表的关系：P1 起 worker 在原写入点后「追加」写本层，
-- sync_job_runs.legacy_ref 回指 sync_runs / integration_sync_runs 的原始行，双写期可对账。
-- 旧表不删、旧页面 API 不动。

-- 作业登记：一行 = 一个可调度的作业。pull（拉取）之外也登记 writeback / reconcile，
-- 否则父件核对这类「写出去」的作业永远进不了告警与新鲜度判定。
CREATE TABLE IF NOT EXISTS sync_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    source_id BIGINT REFERENCES external_sources(id) ON DELETE SET NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    schedule JSONB NOT NULL DEFAULT '{}'::jsonb,
    freshness_sla_seconds INTEGER,
    artifact_glob TEXT,
    alert_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    alert_chat_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_provider_enabled
    ON sync_jobs(provider, enabled);

-- 每次执行。error_kind 是分类而非自由文本：页面和告警直接展示「凭据过期」这类短语，
-- 而不是把 traceback 丢给人自己猜。
CREATE TABLE IF NOT EXISTS sync_job_runs (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL DEFAULT 'schedule',
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    row_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT,
    error_message TEXT,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    legacy_ref JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_sync_job_runs_job_started
    ON sync_job_runs(job_id, started_at DESC);

-- 首屏「最后成功时间 / 新鲜度」是热路径，单独给成功行一条偏索引。
CREATE INDEX IF NOT EXISTS idx_sync_job_runs_job_success
    ON sync_job_runs(job_id, finished_at DESC)
    WHERE status = 'success';

-- 步骤：现在完全缺失的一层。没有它，失败只能看到「退出码 1」，
-- 看不出是取 token 失败、分页第 7 页 429、还是写库失败。
CREATE TABLE IF NOT EXISTS sync_job_steps (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES sync_job_runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    items INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_job_steps_run_seq
    ON sync_job_steps(run_id, seq);

-- 告警状态机。下面那条 partial unique index 是防刷屏的根：
-- 一个作业一种告警同时只可能有一条 open，P3 的 notifier 靠
-- 「INSERT ... ON CONFLICT DO NOTHING 抢占成功才推送」保证不重复推。
CREATE TABLE IF NOT EXISTS sync_job_alerts (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    alert_kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_notified_at TIMESTAMPTZ,
    notify_count INTEGER NOT NULL DEFAULT 0,
    resolved_at TIMESTAMPTZ,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_job_alerts_open
    ON sync_job_alerts(job_id, alert_kind)
    WHERE state = 'open';

CREATE INDEX IF NOT EXISTS idx_sync_job_alerts_state_seen
    ON sync_job_alerts(state, first_seen_at DESC);
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_migration_sync_job_platform -v`
Expected: PASS，7 个测试全绿。

- [ ] **Step 5: 真实跑一次迁移（不能只靠文本断言）**

Run:
```bash
docker compose -f local/docker-compose.local.yml up -d postgres
docker compose -f local/docker-compose.local.yml exec -T postgres \
  psql -U aliecs -d aliecs -f - < db/migrations/0048_sync_job_platform.sql
```
Expected: 输出多行 `CREATE TABLE` / `CREATE INDEX`，无 ERROR。

再跑**第二遍**同一条命令，验证可重复执行：
Expected: 同样无 ERROR（`IF NOT EXISTS` 全部跳过）。

验证表结构真的建出来了：
```bash
docker compose -f local/docker-compose.local.yml exec -T postgres \
  psql -U aliecs -d aliecs -c "\d sync_job_alerts"
```
Expected: 能看到 `idx_sync_job_alerts_open` 且带 `WHERE (state = 'open'::text)` 谓词。

**若 Docker 不可用**：不要跳过后声称已验证。在提交信息与 PR 中写明「未跑真实迁移，原因：<具体原因>」，并列出上述命令供后续补跑。

- [ ] **Step 6: 提交**

```bash
git add db/migrations/0048_sync_job_platform.sql tests/test_migration_sync_job_platform.py
git commit -m "feat(db): 统一同步平台元数据层四表(P0)"
```

---

### Task 2: 抽出 `common/admin.css`

**Files:**
- Create: `services/public-web/common/admin.css`
- Modify: `services/public-web/exports/index.html:7-10`（`<style>` 块）
- Modify: `services/public-web/tplus-sync/index.html:7-10`（`<style>` 块）
- Test: `tests/test_common_admin_assets.py`

**Interfaces:**
- Consumes: 无
- Produces: `/common/admin.css`，供两页及 P2 新建的 `/sync/` 页引用。类名契约（P2 会直接用）：`.wrap` `.topbar` `.band` `.panel` `.grid` `.btn` `.btn.primary` `.chip` `.ok` `.degraded` `.warning` `.critical` `.failed` `.muted` `.metric` `.modal` `.modal-panel` `.row` `.list` `.attention` `.hidden`。

- [ ] **Step 1: 先核对两页 CSS 仍逐字相同**

抽取的前提是两份完全一致；若期间有人改过其中一页，直接合并会引入回归。

共享 CSS 占两行：第 8 行是 `:root{…}` 变量，第 9 行是其余全部规则。两行都要核对。

Run:
```bash
cd services/public-web
sed -n '8,9p' exports/index.html | md5sum
sed -n '8,9p' tplus-sync/index.html | md5sum
```
Expected: 两个 md5 **完全相同**（2026-08-11 单核对第 9 行的值为 `b217917b37e031df6c72898f41dcf3e6`，两行合并后的值请以本次实际输出为准，只要两页一致即可）。

**若不同**：停止本任务，先 diff 出差异并向用户报告，不要自行取舍。

- [ ] **Step 2: 写失败的测试**

创建 `tests/test_common_admin_assets.py`：

```python
from __future__ import annotations

import unittest
from pathlib import Path


PUBLIC_WEB = Path(__file__).resolve().parents[1] / "services" / "public-web"
ADMIN_CSS = PUBLIC_WEB / "common" / "admin.css"
EXPORTS_PAGE = PUBLIC_WEB / "exports" / "index.html"
TPLUS_PAGE = PUBLIC_WEB / "tplus-sync" / "index.html"


class AdminCssTests(unittest.TestCase):
    def test_admin_css_exists(self) -> None:
        self.assertTrue(ADMIN_CSS.is_file(), "common/admin.css 缺失")

    def test_admin_css_carries_the_class_contract(self) -> None:
        # P2 的 /sync/ 页会直接用这些类，抽取时不能漏掉任何一个。
        css = ADMIN_CSS.read_text(encoding="utf-8")
        for selector in (".topbar", ".band", ".btn", ".chip", ".modal", ".hidden", ".muted"):
            self.assertIn(selector, css, f"admin.css 缺少 {selector}")

    def test_both_pages_link_admin_css(self) -> None:
        for page in (EXPORTS_PAGE, TPLUS_PAGE):
            html = page.read_text(encoding="utf-8")
            self.assertIn('<link rel="stylesheet" href="/common/admin.css"/>', html,
                          f"{page.name} 未引用 admin.css")

    def test_pages_no_longer_inline_the_shared_css(self) -> None:
        # 抽取的意义就是不留第二份；留着就会各自漂移。
        for page in (EXPORTS_PAGE, TPLUS_PAGE):
            html = page.read_text(encoding="utf-8")
            self.assertNotIn("--bg:#f7f5f0", html,
                             f"{page.name} 仍内联着共享 CSS 变量")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 跑测试确认它失败**

Run: `python -m unittest tests.test_common_admin_assets -v`
Expected: FAIL —— `common/admin.css 缺失`。

- [ ] **Step 4: 抽出 CSS 文件**

从现有页面原样提取（不要手抄，避免引入差异）：

```bash
cd services/public-web
{ sed -n '8p' exports/index.html; sed -n '9p' exports/index.html; } > common/admin.css
```

（第 8 行是 `:root{...}` 变量定义，第 9 行是其余全部规则；两行都要。）

Run: `head -c 120 common/admin.css`
Expected: 以 `:root{--bg:#f7f5f0` 开头。

- [ ] **Step 5: 两页改为引用**

在 `services/public-web/exports/index.html` 中，把第 7–10 行的整个 `<style>…</style>` 块替换为：

```html
  <link rel="stylesheet" href="/common/admin.css"/>
```

在 `services/public-web/tplus-sync/index.html` 中做**完全相同**的替换。

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m unittest tests.test_common_admin_assets -v`
Expected: PASS，4 个测试全绿。

Run: `python -m unittest discover -s tests`
Expected: 全绿。（本步只动 CSS，不碰 JS 函数名，现有前端测试不应受影响。）

- [ ] **Step 7: 提交**

```bash
git add services/public-web/common/admin.css services/public-web/exports/index.html services/public-web/tplus-sync/index.html tests/test_common_admin_assets.py
git commit -m "refactor(public-web): 抽出 common/admin.css，两页停止各存一份(P0)"
```

---

### Task 3: 抽出 `common/admin-auth.js`

**Files:**
- Create: `services/public-web/common/admin-auth.js`
- Modify: `services/public-web/exports/index.html`（内联脚本头部 + `applyGate`）
- Modify: `services/public-web/tplus-sync/index.html`（内联脚本头部 + `applyGate`）
- Modify: `tests/test_exports_frontend.py:20-21`（`test_has_download_export`）
- Modify: `tests/test_tplus_sync_frontend.py:37-38`（`test_has_download_export`）
- Test: `tests/test_common_admin_assets.py`（追加类）

**Interfaces:**
- Consumes: Task 2 的 `/common/admin.css`（无代码依赖，仅同目录）
- Produces: 全局对象 `window.AliECSAdmin`，字段与签名如下，P2 的 `/sync/` 页直接复用：
  - `API_BASE: string`
  - `token(): string`
  - `authHeaders(): Record<string,string>`
  - `api(path: string, opt?: object): Promise<object>` — 非 2xx 抛 `Error(detail)`
  - `fetchMe(): Promise<object|null>`
  - `isAdminUser(me: object|null): boolean`
  - `applyGate(me: object|null, onAdmin?: () => void): void` — 切换登录/退出/刷新按钮与内容区显隐；**仅当是管理员时**调用 `onAdmin`
  - `downloadExport(url: string, name: string): Promise<void>`
  - `clearAuthToken(): void`
  - `ssoLogin(): void`
  - `esc(v: unknown): string`、`fmtTime(v: unknown): string`、`chip(status: string): string`

- [ ] **Step 1: 核对可抽取的函数确实逐字相同**

Run:
```bash
cd services/public-web
for f in "authHeaders" "async function api(" "async function downloadExport("; do
  echo "[$f]"
  grep -A3 -F "$f" exports/index.html | md5sum
  grep -A3 -F "$f" tplus-sync/index.html | md5sum
done
```
Expected: 每组两个 md5 相同。

**注意 `applyGate` 两页并不相同**（各自调用不同的加载函数），这是已知情况，Step 4 用回调参数处理，不要试图直接合并。

- [ ] **Step 2: 写失败的测试**

在 `tests/test_common_admin_assets.py` 末尾（`if __name__` 之前）追加：

```python
ADMIN_JS = PUBLIC_WEB / "common" / "admin-auth.js"


class AdminAuthJsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.js = ADMIN_JS.read_text(encoding="utf-8") if ADMIN_JS.is_file() else ""

    def test_admin_auth_js_exists(self) -> None:
        self.assertTrue(ADMIN_JS.is_file(), "common/admin-auth.js 缺失")

    def test_exports_the_documented_surface(self) -> None:
        # P2 的 /sync/ 页按这张契约表调用，少一个就得再抽一次。
        for name in ("API_BASE", "token", "authHeaders", "api", "fetchMe",
                     "isAdminUser", "applyGate", "downloadExport",
                     "clearAuthToken", "ssoLogin", "esc", "fmtTime", "chip"):
            self.assertIn(name, self.js, f"AliECSAdmin 缺少 {name}")

    def test_apply_gate_takes_a_callback(self) -> None:
        # 两页的 applyGate 唯一差异就是管理员分支加载什么，用回调收敛。
        self.assertIn("function applyGate(me, onAdmin)", self.js)

    def test_both_pages_load_admin_auth_before_use(self) -> None:
        for page in (EXPORTS_PAGE, TPLUS_PAGE):
            html = page.read_text(encoding="utf-8")
            self.assertIn('<script src="/common/admin-auth.js"></script>', html,
                          f"{page.name} 未引用 admin-auth.js")

    def test_pages_no_longer_define_shared_helpers(self) -> None:
        for page in (EXPORTS_PAGE, TPLUS_PAGE):
            html = page.read_text(encoding="utf-8")
            self.assertNotIn("async function api(", html,
                             f"{page.name} 仍自带一份 api()")
            self.assertNotIn("async function downloadExport(", html,
                             f"{page.name} 仍自带一份 downloadExport()")

    def test_pages_still_pass_their_own_admin_loader(self) -> None:
        # 抽取不能把「登录后加载什么」丢掉。
        exports_html = EXPORTS_PAGE.read_text(encoding="utf-8")
        self.assertIn("loadDocSyncConfig", exports_html)
        self.assertIn("loadExports", exports_html)
        tplus_html = TPLUS_PAGE.read_text(encoding="utf-8")
        self.assertIn("loadSyncConfig", tplus_html)
        self.assertIn("loadTplusTimeline", tplus_html)
```

- [ ] **Step 3: 跑测试确认它失败**

Run: `python -m unittest tests.test_common_admin_assets -v`
Expected: FAIL —— `common/admin-auth.js 缺失`。

- [ ] **Step 4: 写共享脚本**

创建 `services/public-web/common/admin-auth.js`：

```javascript
// 管理页共用的鉴权与请求封装。
// 抽取前 /exports/ 与 /tplus-sync/ 各自内联了逐字相同的一份；新增 /sync/ 时会变成第三份，故收敛到此。
// applyGate 是两页唯一有差异的函数（管理员分支加载的东西不同），用 onAdmin 回调收敛。
(function (global) {
  'use strict';

  const API_BASE = location.port === '8080' ? 'http://localhost:8000' : '/api';
  const AUTH_KEYS = ['aliecs_auth_token', 'portal_token', 'admin_token'];

  const token = () => AUTH_KEYS.map((key) => localStorage.getItem(key) || '').find(Boolean) || '';
  const clearAuthToken = () => AUTH_KEYS.forEach((key) => localStorage.removeItem(key));
  const fmtTime = (value) => (value ? new Date(value).toLocaleString() : '-');
  const chip = (status) => `<span class="chip ${status || 'degraded'}">${status || 'unknown'}</span>`;
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));

  function authHeaders() {
    const headers = { 'Content-Type': 'application/json' };
    const value = token();
    if (value) headers.Authorization = `Bearer ${value}`;
    return headers;
  }

  async function api(path, opt = {}) {
    const response = await fetch(`${API_BASE}${path}`, {
      ...opt,
      headers: Object.assign(authHeaders(), opt.headers || {}),
    });
    const text = await response.text();
    let data = {};
    if (text) { try { data = JSON.parse(text); } catch { data = { raw: text }; } }
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
  }

  async function fetchMe() {
    if (!token()) return null;
    try { return await api('/v1/auth/me'); } catch { return null; }
  }

  const isAdminUser = (me) => !!me
    && (((me.roles || []).includes('admin')) || ((me.permissions || []).includes('admin.access')));

  const ssoLogin = () => {
    location.href = `${API_BASE}/v1/auth/oidc/login?rd=${encodeURIComponent(location.pathname + location.search)}`;
  };

  // 管理员闸门。DOM id 契约：loginBtn / logoutBtn / adminContent / gateHint / refreshBtn(可选)。
  function applyGate(me, onAdmin) {
    const $ = (id) => document.getElementById(id);
    const admin = isAdminUser(me);
    $('loginBtn').classList.toggle('hidden', admin);
    $('logoutBtn').classList.toggle('hidden', !token());
    $('adminContent').classList.toggle('hidden', !admin);
    $('gateHint').classList.toggle('hidden', admin);
    const refresh = $('refreshBtn');
    if (refresh) refresh.classList.toggle('hidden', !admin);
    if (admin && typeof onAdmin === 'function') onAdmin();
  }

  async function downloadExport(url, name) {
    const response = await fetch(`${API_BASE}${url}`, { headers: authHeaders() });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch { /* 保持默认 detail */ }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename\*=UTF-8''([^;]+)/);
    const fileName = match
      ? decodeURIComponent(match[1])
      : (name && name.endsWith('.xlsx') ? name : `${name || 'export'}.xlsx`);
    const anchor = document.createElement('a');
    anchor.href = URL.createObjectURL(blob);
    anchor.download = fileName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(anchor.href);
  }

  global.AliECSAdmin = {
    API_BASE, token, clearAuthToken, authHeaders, api, fetchMe,
    isAdminUser, applyGate, downloadExport, ssoLogin, esc, fmtTime, chip,
  };
})(window);
```

**注意** `downloadExport` 在此处**抛出**异常而不是自己弹 toast（原两页版本内部 catch 后调 `showError`）。调用方负责提示，见 Step 5。

- [ ] **Step 5: 两页改用共享脚本**

在两页中，把 `<script src="/common/toast.js"></script>` 那一行改为两行：

```html
  <script src="/common/toast.js"></script>
  <script src="/common/admin-auth.js"></script>
```

然后在两页内联 `<script>` 的开头，删除这些**已抽走**的定义：
`API_BASE`、`AUTH_KEYS`、`token`、`fmtTime`、`chip`、`esc`、`authHeaders`、`async function api(`、`clearAuthToken`、`fetchMe`、`isAdminUser`、`ssoLogin`、`async function downloadExport(`、以及原来的 `function applyGate(me)`。

替换为：

```javascript
    const {API_BASE, token, api, fetchMe, isAdminUser, esc, fmtTime, chip, clearAuthToken, ssoLogin} = AliECSAdmin;
    const $=(id)=>document.getElementById(id);
    // onclick 内联调用需要挂到 window；失败提示由调用方负责（共享版只抛不弹）。
    window.downloadExport=(url,name)=>AliECSAdmin.downloadExport(url,name).catch((e)=>showError(`下载失败：${e.message}`));
```

**保留不抽的**：`showError` / `showSuccess` / `clearMessage` 三个薄封装留在页面内。它们只是 `AliECSToast` 的一行转发，抽走会让共享脚本反过来依赖 `toast.js` 的加载顺序，得不偿失。

`applyGate` 的调用点改为传回调 —— `services/public-web/exports/index.html` 末尾：

```javascript
    (async()=>{AliECSAdmin.applyGate(await fetchMe(),()=>{
      loadDocSyncConfig();
      loadExports().catch((e)=>{$('exportList').innerHTML=`<span class="chip failed">导出目录加载失败：${esc(e.message)}</span>`;});
    });})();
```

`services/public-web/tplus-sync/index.html` 末尾：

```javascript
    (async()=>{AliECSAdmin.applyGate(await fetchMe(),()=>{loadSyncConfig();loadTplusTimeline();});})();
```

两页的 `logoutBtn.onclick` 里也调了 `applyGate(null)`，一并改为 `AliECSAdmin.applyGate(null)`（登出不需要回调）。

- [ ] **Step 6: 改两个会被抽取打挂的现有测试**

`tests/test_exports_frontend.py` 与 `tests/test_tplus_sync_frontend.py` 都有断言 `"function downloadExport("` 的 `test_has_download_export`，函数抽走后必挂。两个文件中都把该方法体替换为：

```python
    def test_has_download_export(self) -> None:
        # downloadExport 已抽到 /common/admin-auth.js，页面只保留挂载与错误提示。
        self.assertIn('<script src="/common/admin-auth.js"></script>', self.html)
        self.assertIn("window.downloadExport=", self.html)
```

- [ ] **Step 7: 跑全部测试**

Run: `python -m unittest discover -s tests`
Expected: 全绿。若有其他测试因抽取而挂，逐个按"断言改为检查引用共享资产"的思路修，**不要**为了让测试过而把函数抄回页面。

- [ ] **Step 8: 提交**

```bash
git add services/public-web/common/admin-auth.js services/public-web/exports/index.html services/public-web/tplus-sync/index.html tests/test_common_admin_assets.py tests/test_exports_frontend.py tests/test_tplus_sync_frontend.py
git commit -m "refactor(public-web): 抽出 common/admin-auth.js，applyGate 改回调式(P0)"
```

---

### Task 4: 本地 smoke 与文档闭环

**Files:**
- Modify: `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md`（第 12 节实施记录）
- 可能 Modify: `docs/project-navigation.md` 或 `docs/project-ai-map.md`（取决于 `check_navigation.py` 的判定）

**Interfaces:**
- Consumes: Task 1–3 的全部产出
- Produces: 无代码接口；产出的是 P1 接手所需的进度记录

- [ ] **Step 1: 起本地栈**

Run:
```bash
docker compose -f local/docker-compose.local.yml config > /dev/null
docker compose -f local/docker-compose.local.yml up -d
```
Expected: config 无输出即通过；容器全部起来。

- [ ] **Step 2: 两页核心入口 smoke（AGENTS.md 强制要求）**

浏览器打开 `http://localhost:8080/exports/` 与 `http://localhost:8080/tplus-sync/`，各做三件事：

1. 页面样式正常（卡片圆角、米色背景、按钮是胶囊形）——验证 `admin.css` 生效
2. 打开 DevTools Console，确认**无** `AliECSAdmin is not defined` 或 `Uncaught ReferenceError`
3. 点一次右上角「登录（SSO）」，确认跳转发起（Network 里能看到 `/v1/auth/oidc/login` 请求）——验证 `ssoLogin` 与 `applyGate` 仍工作

Expected: 三项全过。这是本阶段唯一能证明"零行为变化"的证据，不能跳过。

- [ ] **Step 3: 跑导航一致性检查**

Run:
```bash
python scripts/check_navigation.py
python -m unittest discover -s tests -p "test_*navigation*.py"
```
Expected: 通过。若报缺失，按其提示补 `docs/project-navigation.md` 或 `docs/project-ai-map.md` 中新增文件的登记。

- [ ] **Step 4: 在 spec 里追加实施记录**

编辑 `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md` 第 12 节，把 `（待填）` 替换为：

```markdown
| 阶段 | 日期 | PR | 验证结论 |
|---|---|---|---|
| P0 建表 + 抽 common 前端资产 | 2026-08-11 | #<PR号> | 迁移 0048 本地跑通且可重复执行；两页 smoke 通过（样式正常、无 JS 报错、SSO 跳转正常）；`unittest discover -s tests` 全绿 |
```

- [ ] **Step 5: 提交并开 PR**

```bash
git add docs/superpowers/specs/2026-08-11-unified-sync-center-design.md
git commit -m "docs(spec): 记录 P0 实施结果"
git push -u origin <当前分支名>
gh pr create --title "feat: 统一同步平台 P0 — 元数据表与前端共享资产" --body "$(cat <<'EOF'
统一同步平台的地基阶段，**零行为变化**：只加表不改表、不写数据、无代码读写；
前端只把两页逐字相同的资产合成一份，渲染与交互结果不变。

## 内容

- 迁移 `0048_sync_job_platform.sql`：`sync_jobs` / `sync_job_runs` / `sync_job_steps` / `sync_job_alerts`
- 抽出 `common/admin.css` 与 `common/admin-auth.js`，`/exports/` 与 `/tplus-sync/` 改为引用
- `applyGate` 改回调式（两页唯一有差异的函数，用 `onAdmin` 收敛）

## 验证

- `python -m unittest discover -s tests` 全绿
- 迁移在本地库跑两遍均无 ERROR；`\d sync_job_alerts` 可见带 `WHERE (state = 'open'::text)` 的唯一索引
- 两页 smoke：样式正常、Console 无报错、SSO 跳转正常

设计：`docs/superpowers/specs/2026-08-11-unified-sync-center-design.md`

Nav-Impact: <updated 或 none>
Nav-Impact-Reason: <若为 none 则填依据>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

PR 正文必须包含 `Nav-Impact: updated`，或同时包含 `Nav-Impact: none` 与 `Nav-Impact-Reason: <依据>`（`ci.yml` 会检查，缺了 CI 直接红）。上面模板里那两处尖括号必须按 Step 3 的实际结果替换，不能原样提交。

- [ ] **Step 6: 等 CI 绿后合并**

Run: `gh pr checks --watch`
Expected: 全绿后 `gh pr merge --squash`。

**注意**：合并到 main 会触发 `release-deploy`，但本阶段是「建表 + 静态资源」，需按 `docs/runbooks/deploy.md` 确认迁移已在目标机执行、`public-web` 容器已换新镜像。判据是 `stage-business-cn-peer` job + 容器双证据，**不是** push 成功本身。

---

## 阶段完成判据

四项全部满足才算 P0 完成、可以进 P1：

1. `python -m unittest discover -s tests` 全绿
2. 迁移 0048 在本地库跑过**两遍**均无 ERROR，`\d sync_job_alerts` 能看到带 `WHERE (state = 'open'::text)` 的唯一索引
3. `/exports/` 与 `/tplus-sync/` 两页 smoke 通过：样式正常、Console 无报错、SSO 跳转正常
4. spec 第 12 节已记录本阶段结果（P1 接手时靠它判断进度）

## 明确不在 P0 范围内

以下都属于 P1 及之后，本阶段**不要**顺手做：

- 往 `sync_jobs` 写任何数据（含 seed）
- 任何 worker 的双写改造
- 新建 `/sync/` 页面或任何 `/v1/sync/*` 接口
- 动 `ops.py` 的两个告警 loop
- 动调度逻辑（`sync_schedule.py` / `worker_loop.py`）
- 删除或重定向 `/tplus-sync/`、瘦身 `/exports/`
