# Unified Sync Center P2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 上线管理员只读的 `/sync/` 统一同步中心，用 P1 的 `sync_jobs` / `sync_job_runs` / `sync_job_steps` / `sync_job_alerts` 展示作业总览、全局时间线和步骤详情，同时保持两个旧页面与全部写入入口不变。

**Architecture:** backend-api 新增独立的 `sync_read.py` 只读查询层与 `routers/sync.py` 管理员 GET 路由；public-web 新增静态 `/sync/` 页面，复用 P0 的 `admin.css`、`admin-auth.js`、`toast.js`。页面用一个全局 runs GET 支撑跨作业分页筛选，并保留 spec 指定的按 job runs GET；运行中的详情每 3 秒轮询，P2 不提供任何 POST/PUT。

**Tech Stack:** Python 3.12、FastAPI、psycopg 3、PostgreSQL 16、原生 HTML/CSS/JavaScript、`unittest`、Docker Compose、GitHub Actions。

## Global Constraints

- P2 是**只读阶段**：只新增 GET；不得实现 `POST /v1/sync/jobs/{key}/run`、`PUT /v1/sync/jobs/{key}/config`，不得创建 request/run。
- `/exports/` 与 `/tplus-sync/` 在 P2 完全不动；重定向和瘦身属于 P5。
- 不改 worker、调度、notifier、`ops.py` 旧告警线程、业务数据表、formula 查询链路、T+ 写回开关或生产密钥。
- 全部 `/v1/sync/*` 路由使用 `Depends(require_admin)`；静态页可公开加载，但未登录不得取得数据。
- `freshness_sla_seconds IS NULL` 必须返回 `unmonitored`；不得从 legacy config 猜 SLA。`schedule` 为空时 `next_expected_at=null`。
- 新鲜度字面量固定为 `fresh | warning | stale | never | unmonitored`；`warning` 为 age ≥ SLA 80% 且 age ≤ SLA。
- error kind 中文映射固定：`auth=凭据过期`、`rate_limit=请求限流`、`network=网络异常`、`schema=数据结构变化`、`write=写入失败`、`unknown=未知错误`。
- formula 产出物只调用现有 `locate_recipe_source()` 读取当前 BOM 文件名与 mtime；找不到时返回 `null`，不得创建、删除或修改文件。
- P3 前 `sync_job_alerts` 可能为空；页面必须有空态，不得伪造告警。
- 全局时间线需要跨 job 正确排序/分页，故新增只读 `GET /v1/sync/runs`；不得用前端 N+1 拼接分页。
- 企微/飞书/T+ job key、`legacy_ref`、`detail_json` 原样只读；API 不返回 `external_sources.external_doc_id`、chat id、凭据或 provider 原始异常。
- 已核查剩余内联 CSS：只有两个库存页 style block 相同，不存在“五页逐字相同”；P2 不做 CSS 重构。
- 测试命令统一使用 `python -m unittest discover -s tests -p "test_xxx.py"`；不得使用 `python -m unittest tests.<module>`。
- AliECS 走 `codex/` 分支 + ready PR；全部 git 写操作串行，显式 `git add -- <files>`，不得 `git add -A`。

---

### Task 1: 只读模型、新鲜度与 formula 当前产出物

**Files:**
- Create: `services/backend-api/app/sync_read.py`
- Create: `tests/test_backend_sync_read.py`

**Interfaces:**
- Produces: `classify_freshness(last_success_at, sla_seconds, *, now=None) -> dict[str, Any]`
- Produces: `formula_bom_artifact() -> dict[str, Any] | None`
- Produces: `error_kind_label(error_kind: str | None) -> str`
- Later tasks consume: `overview(conn, *, now=None)`, `runs_page(...)`, `run_detail(...)`, `alerts_page(...)` added to the same module.

- [ ] **Step 1: 写失败的新鲜度与产出物测试**

```python
class FreshnessTests(unittest.TestCase):
    def test_null_sla_is_unmonitored(self):
        value = sync_read.classify_freshness(datetime(2026, 8, 12, tzinfo=timezone.utc), None,
                                             now=datetime(2026, 8, 13, tzinfo=timezone.utc))
        self.assertEqual({"state": "unmonitored", "sla_seconds": None,
                          "age_seconds": None, "ratio": None}, value)

    def test_never_run_is_distinct_from_stale(self):
        value = sync_read.classify_freshness(None, 3600,
                                             now=datetime(2026, 8, 13, tzinfo=timezone.utc))
        self.assertEqual("never", value["state"])

    def test_warning_starts_at_eighty_percent(self):
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        value = sync_read.classify_freshness(now - timedelta(seconds=2880), 3600, now=now)
        self.assertEqual("warning", value["state"])

    def test_stale_is_strictly_past_sla(self):
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        self.assertEqual("fresh", sync_read.classify_freshness(now - timedelta(seconds=2879), 3600, now=now)["state"])
        self.assertEqual("warning", sync_read.classify_freshness(now - timedelta(seconds=3600), 3600, now=now)["state"])
        self.assertEqual("stale", sync_read.classify_freshness(now - timedelta(seconds=3601), 3600, now=now)["state"])

class FormulaArtifactTests(unittest.TestCase):
    def test_reports_exact_file_selected_by_formula(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("RECIPE_BOM_INPUT_DIR")
            os.environ["RECIPE_BOM_INPUT_DIR"] = tmp
            path = Path(tmp) / "bom_20260812_020000.xlsx"
            path.write_bytes(b"test")
            os.utime(path, (1_786_476_000, 1_786_476_000))
            try:
                artifact = sync_read.formula_bom_artifact()
            finally:
                restore_env("RECIPE_BOM_INPUT_DIR", old)
            self.assertEqual(path.name, artifact["name"])
            self.assertEqual(int(path.stat().st_mtime), artifact["mtime_epoch"])

    def test_missing_formula_input_returns_none_without_creating_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"RECIPE_BOM_INPUT_DIR": tmp}):
            self.assertIsNone(sync_read.formula_bom_artifact())
            self.assertEqual([], list(Path(tmp).iterdir()))
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_backend_sync_read.py" -v`

Expected: FAIL，`app.sync_read` 尚不存在。

- [ ] **Step 3: 实现纯只读 helper**

```python
ERROR_KIND_LABELS = {
    "auth": "凭据过期",
    "rate_limit": "请求限流",
    "network": "网络异常",
    "schema": "数据结构变化",
    "write": "写入失败",
    "unknown": "未知错误",
}

def error_kind_label(error_kind: str | None) -> str:
    return ERROR_KIND_LABELS.get(str(error_kind or "unknown"), "未知错误")

def classify_freshness(last_success_at, sla_seconds, *, now=None):
    if sla_seconds is None:
        return {"state": "unmonitored", "sla_seconds": None, "age_seconds": None, "ratio": None}
    sla = int(sla_seconds)
    if last_success_at is None:
        return {"state": "never", "sla_seconds": sla, "age_seconds": None, "ratio": None}
    current = now or datetime.now(timezone.utc)
    age = max(0, int((current - last_success_at).total_seconds()))
    ratio = age / sla if sla > 0 else None
    state = "stale" if age > sla else ("warning" if age >= sla * 0.8 else "fresh")
    return {"state": state, "sla_seconds": sla, "age_seconds": age, "ratio": ratio}

def formula_bom_artifact():
    try:
        path = locate_recipe_source()
        stat = path.stat()
    except (FileNotFoundError, OSError):
        return None
    return {"name": path.name, "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "mtime_epoch": int(stat.st_mtime)}
```

不得读取 Excel 内容，不得捕获范围外异常后返回路径文本。

- [ ] **Step 4: 验证 helper**

Run: `python -m unittest discover -s tests -p "test_backend_sync_read.py" -v`

Expected: 全绿。

- [ ] **Step 5: 提交**

```powershell
git add -- services/backend-api/app/sync_read.py tests/test_backend_sync_read.py
git commit -m "feat(sync): add read-only sync center model"
```

---

### Task 2: 总览与告警 GET API

**Files:**
- Modify: `services/backend-api/app/sync_read.py`
- Create: `services/backend-api/app/routers/sync.py`
- Modify: `services/backend-api/app/main.py`
- Modify: `tests/test_backend_sync_read.py`
- Create: `tests/test_backend_sync_api.py`

**Interfaces:**
- Produces: `GET /v1/sync/overview`
- Produces: `GET /v1/sync/alerts?state=open&limit=50&offset=0`
- Produces: `overview(conn, *, now=None) -> dict[str, Any]`
- Produces: `alerts_page(conn, *, state, limit, offset) -> dict[str, Any]`

- [ ] **Step 1: 写失败的 SQL/响应与管理员依赖测试**

使用记录 SQL 与 tuple rows 的 `FakeConnection`，至少覆盖：空库；success/failed/running/partial 最近状态；open alert 聚合；NULL SLA；formula artifact 只挂到 `chanjet.full`；所有 route 带 `require_admin`。

```python
def test_overview_shape_and_summary(self):
    result = sync_read.overview(self.conn, now=NOW)
    self.assertEqual({"jobs": 4, "fresh": 1, "warning": 1, "stale": 1,
                      "never": 0, "unmonitored": 1, "failed": 1,
                      "partial": 1, "running": 1, "open_alerts": 2}, result["summary"])
    self.assertEqual("凭据过期", result["items"][0]["last_run"]["error_label"])

def test_overview_sql_uses_lateral_latest_rows(self):
    sync_read.overview(self.conn, now=NOW)
    sql = self.conn.joined_sql()
    self.assertIn("LEFT JOIN LATERAL", sql)
    self.assertIn("status = 'success'", sql)
    self.assertIn("state = 'open'", sql)
    self.assertNotIn("external_doc_id", sql)

def test_alerts_defaults_to_open_and_joins_job(self):
    result = sync_read.alerts_page(self.conn, state="open", limit=50, offset=0)
    self.assertEqual("wecom.doc.17", result["items"][0]["job_key"])
    self.assertEqual("open", result["items"][0]["state"])

def test_sync_get_routes_require_admin(self):
    for path in ("/v1/sync/overview", "/v1/sync/alerts"):
        route = route_for(self.app, path, "GET")
        calls = {dep.call for dep in route.dependant.dependencies}
        self.assertIn(require_admin, calls)
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_backend_sync*.py" -v`

Expected: FAIL，overview/alerts/router 尚不存在。

- [ ] **Step 3: 实现 overview 与 alerts 查询**

overview 主查询必须从 `sync_jobs j` 开始，并用 lateral 取最新 run 与最近 success；告警用预聚合子查询，避免 run × alert 行数放大：

```sql
LEFT JOIN LATERAL (
  SELECT id, trigger, status, started_at, finished_at, row_count, changed_count,
         error_kind, error_message, detail_json, legacy_ref
  FROM sync_job_runs WHERE job_id=j.id
  ORDER BY started_at DESC, id DESC LIMIT 1
) latest ON TRUE
LEFT JOIN LATERAL (
  SELECT finished_at FROM sync_job_runs
  WHERE job_id=j.id AND status='success'
  ORDER BY finished_at DESC NULLS LAST, id DESC LIMIT 1
) succeeded ON TRUE
LEFT JOIN (
  SELECT job_id, COUNT(*) AS open_alert_count
  FROM sync_job_alerts WHERE state='open' GROUP BY job_id
) alerts ON alerts.job_id=j.id
```

返回字段必须包含 Global Constraints 中 overview shape；`next_expected_at` 在 P2 固定为 `null`（不要复制第四份调度算法），原 `schedule` 仍返回供页面显示“未配置”。

alerts 必须使用 count + page 两条参数化 SQL，允许 `state=open|resolved|all`；`all` 时不拼 state predicate，非法 state 在 router 返回 422。

- [ ] **Step 4: 装配 GET router**

```python
router = APIRouter(prefix="/v1/sync", tags=["sync-center"])

@router.get("/overview")
def sync_overview(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        return sync_read.overview(conn)

@router.get("/alerts")
def sync_alerts(state: Literal["open", "resolved", "all"] = "open",
                limit: int = Query(50, ge=1, le=200),
                offset: int = Query(0, ge=0),
                _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        return sync_read.alerts_page(conn, state=state, limit=limit, offset=offset)
```

`_conn` 从 `app.core` 导入；数据库错误统一转为 `HTTPException(500, "读取同步中心失败：<ExceptionType>")`，不得回传 SQL/DSN。

- [ ] **Step 5: 验证并提交**

Run: `python -m unittest discover -s tests -p "test_backend_sync*.py" -v`

```powershell
git add -- services/backend-api/app/sync_read.py services/backend-api/app/routers/sync.py services/backend-api/app/main.py tests/test_backend_sync_read.py tests/test_backend_sync_api.py
git commit -m "feat(sync): expose overview and alert reads"
```

---

### Task 3: 全局时间线、按作业时间线与运行详情

**Files:**
- Modify: `services/backend-api/app/sync_read.py`
- Modify: `services/backend-api/app/routers/sync.py`
- Modify: `tests/test_backend_sync_read.py`
- Modify: `tests/test_backend_sync_api.py`

**Interfaces:**
- Produces: `GET /v1/sync/runs?job_key=&provider=&status=&limit=20&offset=0`
- Produces: `GET /v1/sync/jobs/{job_key}/runs?status=&limit=20&offset=0`
- Produces: `GET /v1/sync/runs/{run_id}`
- Produces: `runs_page(conn, *, job_key, provider, status, limit, offset) -> dict[str, Any]`
- Produces: `run_detail(conn, run_id) -> dict[str, Any] | None`

- [ ] **Step 1: 写失败的分页、筛选与详情测试**

```python
def test_global_runs_filters_are_parameterized(self):
    page = sync_read.runs_page(self.conn, job_key=None, provider="wecom", status="failed",
                               limit=20, offset=40)
    self.assertEqual(87, page["total"])
    sql = self.conn.joined_sql()
    self.assertIn("j.provider = %s", sql)
    self.assertIn("r.status = %s", sql)
    self.assertNotIn("wecom", sql)

def test_runs_have_stable_global_order(self):
    sync_read.runs_page(self.conn, job_key=None, provider=None, status=None, limit=20, offset=0)
    self.assertIn("ORDER BY r.started_at DESC, r.id DESC", self.conn.joined_sql())

def test_run_detail_orders_steps_and_labels_error(self):
    detail = sync_read.run_detail(self.conn, 91)
    self.assertEqual([1, 2, 3], [step["seq"] for step in detail["steps"]])
    self.assertEqual("请求限流", detail["run"]["error_label"])

def test_missing_job_and_run_return_404(self):
    with self.assertRaises(HTTPException) as job_error:
        endpoint_for("/v1/sync/jobs/{job_key}/runs")(job_key="missing", status=None, limit=20, offset=0, _={})
    self.assertEqual(404, job_error.exception.status_code)
```

还要覆盖 status 只接受 `running|success|partial|failed` 或空；provider/job_key 永远作为参数，不拼接进 SQL。

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_backend_sync*.py" -v`

Expected: FAIL，三个 runs GET 尚不存在。

- [ ] **Step 3: 实现共享 runs 查询**

使用一个 predicate builder，仅拼固定 SQL 片段，值全部进 params：

```python
def _run_filters(*, job_key, provider, status):
    clauses, params = [], []
    for column, value in (("j.job_key", job_key), ("j.provider", provider), ("r.status", status)):
        if value:
            clauses.append(f"{column} = %s")
            params.append(value)
    return (" AND ".join(clauses) or "TRUE"), params
```

column 只能来自上面固定 tuple，任何用户输入不得成为 SQL 标识符。count/page 使用同一 predicate/params；page 字段包含 job 摘要、run 全字段、`error_label`、`duration_seconds`。

按 job endpoint 先用参数化 `SELECT 1 FROM sync_jobs WHERE job_key=%s` 判 404，再调用共享 query。

- [ ] **Step 4: 实现 run detail**

先 join job 取 run；无行返回 `None`。再查：

```sql
SELECT seq, name, status, started_at, finished_at, items, message
FROM sync_job_steps WHERE run_id=%s ORDER BY seq ASC
```

若 `job_key='chanjet.full'` 且 `detail_json.full_snapshot_id` 有值，再参数化查询 `integration_reconciliation_diffs` 的 id；不存在时为 null。其他 provider 不查 reconciliation。steps message 按原值只读，不额外拼 raw detail。

- [ ] **Step 5: 验证并提交**

Run: `python -m unittest discover -s tests -p "test_backend_sync*.py" -v`

```powershell
git add -- services/backend-api/app/sync_read.py services/backend-api/app/routers/sync.py tests/test_backend_sync_read.py tests/test_backend_sync_api.py
git commit -m "feat(sync): add unified run timeline reads"
```

---

### Task 4: `/sync/` 总览与跨作业时间线

**Files:**
- Create: `services/public-web/sync/index.html`
- Create: `tests/test_sync_frontend.py`

**Interfaces:**
- Consumes: `/v1/sync/overview`, `/v1/sync/runs`, `/v1/sync/alerts`
- Produces: `/sync/` 首屏汇总、job rows、provider/status/job 筛选与分页。

- [ ] **Step 1: 写失败的静态页面契约测试**

```python
class SyncFrontendTests(unittest.TestCase):
    def setUp(self):
        self.html = SYNC_PAGE.read_text(encoding="utf-8") if SYNC_PAGE.exists() else ""

    def test_has_three_layers_and_summary(self):
        for dom_id in ("syncSummary", "jobList", "timelineList", "runDrawer", "alertList"):
            self.assertIn(f'id="{dom_id}"', self.html)

    def test_uses_read_only_endpoints(self):
        for path in ("/v1/sync/overview", "/v1/sync/runs", "/v1/sync/alerts"):
            self.assertIn(path, self.html)
        self.assertNotIn("method:'POST'", self.html)
        self.assertNotIn("method:'PUT'", self.html)

    def test_has_global_filters_and_query_preselection(self):
        for dom_id in ("providerFilter", "statusFilter", "jobFilter"):
            self.assertIn(f'id="{dom_id}"', self.html)
        self.assertIn("URLSearchParams(location.search)", self.html)
        self.assertIn("job", self.html)
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_sync_frontend.py" -v`

Expected: FAIL，页面不存在。

- [ ] **Step 3: 建页面骨架并复用共享资产**

页面必须有：

```html
<link rel="stylesheet" href="/common/admin.css"/>
...
<button id="loginBtn" class="btn" type="button">登录（SSO）</button>
<button id="logoutBtn" class="btn hidden" type="button">退出登录</button>
<button id="refreshBtn" class="btn primary hidden" type="button">刷新</button>
<div id="gateHint" class="band">此页面仅管理员可见。</div>
<div id="adminContent" class="hidden">...</div>
<script src="/common/toast.js"></script>
<script src="/common/admin-auth.js"></script>
```

不内联 P0 共享 CSS，不定义自己的 `api()` / `fmtTime()` / `chip()` / auth helpers。P2 只读按钮若保留版式，必须是 `disabled` 且 title 明写“后续阶段开放”，不得绑定 handler。

- [ ] **Step 4: 实现 overview/alerts/timeline 渲染**

页面脚本使用：

```javascript
const {api, fetchMe, applyGate, esc, fmtTime, chip, clearAuthToken, ssoLogin} = AliECSAdmin;
const state={overview:null,alerts:[],runs:[],limit:20,offset:0,total:0};

async function loadOverview(){state.overview=await api('/v1/sync/overview');renderOverview();}
async function loadAlerts(){const d=await api('/v1/sync/alerts?state=open&limit=50&offset=0');state.alerts=d.items||[];renderAlerts();}
async function loadTimeline(){
  const params=new URLSearchParams({limit:String(state.limit),offset:String(state.offset)});
  if(providerFilter.value)params.set('provider',providerFilter.value);
  if(statusFilter.value)params.set('status',statusFilter.value);
  if(jobFilter.value)params.set('job_key',jobFilter.value);
  const d=await api(`/v1/sync/runs?${params}`);state.runs=d.items||[];state.total=d.total||0;renderTimeline();
}
```

所有 API 文本用 `esc()`；禁止把 `detail_json` 直接 `JSON.stringify` 倾倒到页面。summary 必须显示 unmonitored，避免 jobs 总数与 fresh/warning/stale 对不上。

- [ ] **Step 5: 验证并提交**

Run: `python -m unittest discover -s tests -p "test_sync_frontend.py" -v`

```powershell
git add -- services/public-web/sync/index.html tests/test_sync_frontend.py
git commit -m "feat(sync): add read-only sync center page"
```

---

### Task 5: 详情抽屉、步骤瀑布、3 秒轮询与入口契约

**Files:**
- Modify: `services/public-web/sync/index.html`
- Modify: `services/public-web/health/index.html`
- Modify: `tests/test_sync_frontend.py`
- Modify: `tests/test_common_admin_assets.py`
- Modify: `tests/test_frontend_toast.py`
- Modify: `tests/test_health_frontend.py`

**Interfaces:**
- Consumes: `GET /v1/sync/runs/{id}`
- Produces: run detail drawer、步骤 waterfall、running-only polling、health 入口。

- [ ] **Step 1: 写失败的详情、轮询与共享资产测试**

```python
def test_detail_drawer_loads_run_and_steps(self):
    self.assertIn("function openRunDetail(", self.html)
    self.assertIn("/v1/sync/runs/${runId}", self.html)
    self.assertIn("function renderSteps(", self.html)
    self.assertIn("error_label", self.html)

def test_running_detail_polls_every_three_seconds_and_stops(self):
    self.assertIn("3000", self.html)
    self.assertIn("detailPollTimer", self.html)
    self.assertIn("clearInterval(detailPollTimer)", self.html)
    self.assertIn("if(d.run.status==='running')", self.html)

def test_sync_page_uses_shared_admin_contract(self):
    for marker in ('href="/common/admin.css"', 'src="/common/admin-auth.js"',
                   'id="loginBtn"', 'id="logoutBtn"', 'id="adminContent"', 'id="gateHint"'):
        self.assertIn(marker, self.html)
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python -m unittest discover -s tests -p "test_sync_frontend.py" -v`

Expected: FAIL，详情与轮询尚未实现。

- [ ] **Step 3: 实现详情与 polling 生命周期**

```javascript
let detailPollTimer=null, openRunId=null;
function stopDetailPolling(){if(detailPollTimer){clearInterval(detailPollTimer);detailPollTimer=null;}}
async function openRunDetail(runId){
  openRunId=runId;stopDetailPolling();runDrawer.classList.add('show');
  const d=await api(`/v1/sync/runs/${runId}`);renderRunDetail(d);
  if(d.run.status==='running'){
    detailPollTimer=setInterval(()=>refreshOpenRun().catch(showError),3000);
  }
}
async function refreshOpenRun(){
  if(!openRunId)return;
  const d=await api(`/v1/sync/runs/${openRunId}`);renderRunDetail(d);
  if(d.run.status!=='running'){stopDetailPolling();await Promise.all([loadOverview(),loadTimeline()]);}
}
```

关闭抽屉、页面 `beforeunload`、切换 run 时都调用 `stopDetailPolling()`。step duration 由 started/finished 计算；running 用“进行中”，失败 step 显示 message，成功 step 显示 items。

- [ ] **Step 4: 纳入共享资产与 health 导航契约**

`test_common_admin_assets.py` 的页面集合加入 `SYNC_PAGE`；`test_frontend_toast.py` 加 `public-web/sync/index.html`；health 添加只读卡片：

```html
<a class="card-link" href="/sync/">
  <article class="card"><h3>统一同步中心</h3><p>全部文档与 T+ 作业的运行和步骤。</p></article>
</a>
```

不要删除或改写现有 `/exports/`、`/tplus-sync/` 两张卡。

- [ ] **Step 5: 验证并提交**

Run: `python -m unittest discover -s tests -p "test_sync_frontend.py" -v`

Run: `python -m unittest discover -s tests -p "test_common_admin_assets.py" -v`

Run: `python -m unittest discover -s tests -p "test_frontend_toast.py" -v`

Run: `python -m unittest discover -s tests -p "test_health_frontend.py" -v`

```powershell
git add -- services/public-web/sync/index.html services/public-web/health/index.html tests/test_sync_frontend.py tests/test_common_admin_assets.py tests/test_frontend_toast.py tests/test_health_frontend.py
git commit -m "feat(sync): add run detail and sync center entry"
```

---

### Task 6: PostgreSQL 只读集成、导航与阶段回归

**Files:**
- Create: `tests/test_sync_read_api_integration.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/project-ai-map.md`

**Interfaces:**
- Consumes: Task 1–5 所有查询与路由。
- Produces: migration-dry-run 中真实 PostgreSQL overview/runs/detail/alerts 证据。

- [ ] **Step 1: 写 opt-in PostgreSQL 集成测试**

测试仅在 `SYNC_JOB_PLATFORM_INTEGRATION=1` 时运行；使用唯一 `ci.p2.<uuid>` job key直接建立**测试夹具**（本测试验证读取层，不调用 P1 writer validator），插入 success/failed runs、3 steps、1 open alert，再调用 `sync_read` 四个函数断言。

```python
def test_reads_overview_timeline_detail_and_alerts(self):
    overview = sync_read.overview(self.conn, now=NOW)
    item = next(row for row in overview["items"] if row["job_key"] == self.job_key)
    self.assertEqual("failed", item["last_run"]["status"])
    self.assertEqual("fresh", item["freshness"]["state"])
    page = sync_read.runs_page(self.conn, job_key=self.job_key, provider=None, status=None, limit=20, offset=0)
    self.assertEqual(2, page["total"])
    detail = sync_read.run_detail(self.conn, self.failed_run_id)
    self.assertEqual([1, 2, 3], [step["seq"] for step in detail["steps"]])
    alerts = sync_read.alerts_page(self.conn, state="open", limit=50, offset=0)
    self.assertIn(self.job_key, [row["job_key"] for row in alerts["items"]])
```

`finally` 按精确 job id 删除（CASCADE 清 run/steps/alerts），不得用 `LIKE 'ci.%'` 扫并发测试。

- [ ] **Step 2: 在 CI 迁移后执行 P2 集成**

在现有 P1 integration step 后、backend smoke 前追加：

```yaml
      - name: Run sync center read integration test
        env:
          DATABASE_URL: postgresql://aliecs:aliecs@127.0.0.1:5432/aliecs
          SYNC_JOB_PLATFORM_INTEGRATION: "1"
        run: python -m unittest discover -s tests -p "test_sync_read_api_integration.py" -v
```

- [ ] **Step 3: 更新导航**

`docs/project-ai-map.md` 增加：`/v1/sync/*` → `app/routers/sync.py` + `app/sync_read.py`；`/sync/` → `services/public-web/sync/index.html`；说明 P2 是只读，不含 run/config 写入口。

- [ ] **Step 4: 运行完整验证**

Run: `python -m unittest discover -s tests`

Run: `python scripts/check_navigation.py`

Run: `docker compose -f local/docker-compose.local.yml config --quiet`

Run: `python -m unittest discover -s tests -p "test_sync_read_api_integration.py" -v`

Expected: 根测试全绿；导航/Compose exit 0；未开 integration env 时 1 skipped。

- [ ] **Step 5: 提交**

```powershell
git add -- tests/test_sync_read_api_integration.py .github/workflows/ci.yml docs/project-ai-map.md
git commit -m "test(sync): verify P2 read model against PostgreSQL"
```

---

### Task 7: 全分支审查、PR、部署与人工检查清单

**Files:**
- Modify after PR exists: `docs/superpowers/specs/2026-08-11-unified-sync-center-design.md`
- Update ignored ledger: `.superpowers/sdd/2026-08-12-unified-sync-center-p2/progress.md`

**Interfaces:**
- Produces: 已合并、已部署的 P2 只读页面/API；P3 可直接读取相同 alert API。

- [ ] **Step 1: SDD 全分支终审**

review range 为 `merge-base origin/main HEAD..HEAD`。Critical/Important 全部修复并做一次 scoped re-review。明确检查：无 POST/PUT、新旧页面未改、无原始 detail dump、SQL 全参数化、轮询 timer 可停止、权限依赖齐。

- [ ] **Step 2: 最终验证与安全检查**

Run: `python -m unittest discover -s tests`

Run: `python scripts/check_navigation.py`

Run: `docker compose -f local/docker-compose.local.yml config --quiet`

串行确认 `git status --short`、branch、remote、`git diff --name-only origin/main...HEAD`；确认无 `.env`、logs、browser_data、`_references`、`.superpowers`、worktree 文件。

- [ ] **Step 3: 推送 ready PR 并回填 spec**

PR 正文含：只读边界、GET 列表、全局 runs endpoint 的 spec 补全理由、根测试、真实 PG、`Nav-Impact: updated`。拿到真实 URL 后更新 spec 第 12 节 P2 行，显式提交推送；`gh pr checks --watch` 全绿才 squash merge。

- [ ] **Step 4: 部署 business-cn**

```powershell
gh workflow run release-deploy.yml --ref main -f deploy_target=business-cn
```

判据：`stage-business-cn-peer=success`，`deploy-business-cn=skipped` 正常；txecs 的 backend-api 与 public-web 新镜像 tag/image SHA/启动时间刷新。

- [ ] **Step 5: 自动线上验证**

验证：

```text
GET /sync/ -> 200
GET /common/admin.css -> 200
GET /common/admin-auth.js -> 200
未登录 GET /api/v1/sync/overview -> 401
```

使用已有登录会话打开 `/sync/`：零 pageerror、零 console error；作业数与生产 SQL 一致；`?job=chanjet.full` 能筛选；打开 running/terminal run 详情 steps 顺序正确；旧 `/exports/`、`/tplus-sync/` 仍 200 且行为不变。

- [ ] **Step 6: 给用户非阻塞人工检查清单并进入 P3**

人工检查只需：汇总文案、四类作业名称、时间线筛选、详情抽屉、formula 当前文件名是否符合直觉。P2 已有自动/生产证据时，不等待人工回复，直接开始 P3 `writing-plans`；用户反馈作为后续修正。

---

## 阶段完成判据

1. P2 只新增管理员 GET 查询和 `/sync/` 页面；无 POST/PUT，旧 `/exports/`、`/tplus-sync/` 不变。
2. overview、全局/按 job timeline、run detail、alerts 均有单测和 PostgreSQL 16 集成证据。
3. NULL SLA 显示未监控，空 schedule 不猜 next run，P3 前空 alerts 有明确空态。
4. formula 当前 BOM 文件只读展示，不修改 formula 或产出目录。
5. 根 CI、导航、Compose、PR、发布、txecs 镜像与线上页面/API证据齐全。

