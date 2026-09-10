# V6 Market Three-Page Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将市场观察台拆为实时、当日错单、历史复盘三页，并以有界查询降低首屏和持续刷新负载。

**Architecture:** 后端提供轻量索引与按事件详情两类接口；前端保留共享认证/图表工具，三页各自只请求用途所需数据。旧 `/market/` 继续作为实时页入口。

**Tech Stack:** FastAPI、PostgreSQL、原生 JavaScript、Lightweight Charts、pytest、Playwright。

**Spec:** `docs/superpowers/specs/2026-09-10-market-three-page-split-design.md`

## Global Constraints

- 不删除或改写观察、事件、持仓、标注原始证据。
- 实时接口服务端强制最近 15 分钟窗口和最大行数。
- 当日/历史首屏只返回分页索引，详情必须用户选中后加载。
- execute=false；不得连接真实资金账户或发出真实交易指令。
- 所有 Git 命令使用显式 `git -C /home/ishelwsl/src/aliecs-wt-v6-review`。

### Task 1: 后端轻量索引与详情接口

**Files:**
- Modify: `services/backend-api/app/market_review.py`
- Modify: backend route module registering `/api/v1/market`
- Test: `tests/test_market_review_api.py`

**Interfaces:**
- `GET /api/v1/market/realtime?minutes=15` returns `{contracts,quotes,bands,orders,hedge_ranking,window}` with server-side 15-minute cap.
- `GET /api/v1/market/events/index?trading_day=YYYY-MM-DD&scope=today|history&after=&limit=` returns compact event rows and cursor.
- `GET /api/v1/market/events/{event_id}/detail` returns bounded curves and lifecycle evidence for one event.

- [ ] Step 1: Add failing tests for the 15-minute cap, index-only response, and event detail isolation.
- [ ] Step 2: Run `pytest tests/test_market_review_api.py -q` and confirm the new tests fail.
- [ ] Step 3: Implement SQL projections using existing market-review tables and explicit `limit`/cursor bounds; return gap and freshness metadata.
- [ ] Step 4: Run the focused API tests and existing market API tests; confirm pass.
- [ ] Step 5: Commit `feat: add bounded market review page APIs`.

### Task 2: 页面路由与实时轻量页

**Files:**
- Create: `services/public-web/market/realtime.html`
- Create: `services/public-web/market/realtime.js`
- Modify: `services/public-web/market/index.html`
- Modify: `services/public-web/market/market.css`
- Test: `tests/test_market_frontend.py`

- [ ] Step 1: Add frontend contract tests asserting the realtime page calls only `/realtime` and renders all returned contracts.
- [ ] Step 2: Implement the realtime page with a fixed 15-minute client ring buffer, current order markers and hedge ranking; remove event/raw-evidence blocks from its DOM.
- [ ] Step 3: Make `/market/` link/redirect to realtime and add navigation links to today/history.
- [ ] Step 4: Run frontend contract tests and static syntax checks.
- [ ] Step 5: Commit `feat: add lightweight realtime market page`.

### Task 3: 当日与历史复盘页

**Files:**
- Create: `services/public-web/market/today.html`
- Create: `services/public-web/market/history.html`
- Create: `services/public-web/market/review-detail.js`
- Modify: `services/public-web/market/observation.css`
- Test: `tests/test_market_review_browser.py`

- [ ] Step 1: Add browser tests proving today/history initial loads request index only and selection triggers one bounded detail request.
- [ ] Step 2: Implement shared index/detail controller with cursor pagination, loading/error/partial states, and selection cancellation.
- [ ] Step 3: Implement today scope using UTC+08 trading day and history scope excluding today.
- [ ] Step 4: Render selected event curves and lifecycle without dumping raw JSON or unbounded text.
- [ ] Step 5: Run focused Playwright tests on desktop and narrow viewport.
- [ ] Step 6: Commit `feat: split market review pages`.

### Task 4: 集成性能验收与交接

**Files:**
- Modify: `tests/test_market_review_e2e.py`
- Modify: `docs/superpowers/plans/2026-09-10-market-three-page-split.md`
- Modify: `HANDOFF-2026-09-04.md`

- [ ] Step 1: Run the existing market API/frontend/browser suites plus the new three-page tests.
- [ ] Step 2: Record response bytes, row counts, and first-render timings for 8 contracts; verify realtime has zero detail requests before selection.
- [ ] Step 3: Run `git diff --check` and inspect status for unrelated files/secrets.
- [ ] Step 4: Update handoff with exact test counts, deployment boundary, and any unverified production behavior.
- [ ] Step 5: Commit `docs: record three-page market review handoff`.
