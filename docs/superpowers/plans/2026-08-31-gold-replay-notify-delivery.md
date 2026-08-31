# Gold Replay Notification Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 让黄金价差正式复盘使用统一消息中枢展示结构化进度和研究结果，并能用投递记录证明消息是否真正送达。

**Architecture:** 保留 `POST /v1/internal/gold-spread/alerts` 作为黄金价差业务适配器；它只校验业务字段并构造统一 `Notification`，所有路由、幂等、渠道渲染、重试和投递记账仍由 `app/notify/` 完成。重复提交必须读取既有 `notify_deliveries` 的当前状态，通用查询接口按来源隔离地返回投递凭证。

**Tech Stack:** FastAPI、Pydantic v2、PostgreSQL、pytest

**Spec:** `docs/runbooks/notify.md`

## Global Constraints

- 不新增飞书、企微发送实现；渠道代码只保留在 `services/backend-api/app/notify/channels/`。
- 不在仓库保存真实 token、收件人或生产环境地址。
- 不改变 `gold-spread-monitor` 的模型、复盘、MT5 或 supervisor 配置。
- 不新增数据库迁移；复用 `notify_outbox`、`notify_deliveries` 和现有索引。
- `POST /v1/internal/gold-spread/alerts` 的旧请求仍可用；结构化复盘字段为可选字段。

---

### Task 1: Duplicate Delivery Receipt

**Files:**
- Modify: `services/backend-api/app/notify/store.py`
- Modify: `services/backend-api/app/notify/dispatch.py`
- Test: `tests/test_notify_center.py`

**Interfaces:**
- Produces: `delivery_summary(conn, outbox_id: int) -> dict[str, int]`
- Produces: `dispatch.deliver()` 返回 `targets`、`sent`、`pending`、`dead`、`failed`

- [x] **Step 1: Write the failing duplicate receipt test**

```python
def test_duplicate_reports_existing_delivery_state(self):
    conn = FakeConnection(rows=[None, (42,)], rowsets=[[("sent", 1), ("pending", 1)]])
    result = dispatch.deliver(make_notification(), conn=conn)
    self.assertTrue(result["duplicate"])
    self.assertEqual(result["targets"], 2)
    self.assertEqual(result["sent"], 1)
    self.assertEqual(result["pending"], 1)
```

- [x] **Step 2: Run the test and confirm it fails because duplicate results are all zero**

Run: `.venv/bin/python -m pytest -q tests/test_notify_center.py::DispatchTests::test_duplicate_reports_existing_delivery_state`

- [x] **Step 3: Implement one grouped delivery-status query and use it for duplicate outbox rows**

```python
def delivery_summary(conn, outbox_id: int) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) FROM notify_deliveries "
            "WHERE outbox_id = %s GROUP BY status",
            (outbox_id,),
        )
        counts = {str(status): int(count) for status, count in cur.fetchall()}
    pending, dead = counts.get("pending", 0), counts.get("dead", 0)
    return {
        "targets": sum(counts.values()), "sent": counts.get("sent", 0),
        "pending": pending, "dead": dead, "failed": pending + dead,
    }
```

- [x] **Step 4: Run notify-center tests**

Run: `.venv/bin/python -m pytest -q tests/test_notify_center.py`

### Task 2: Generic Delivery Status Endpoint

**Files:**
- Modify: `services/backend-api/app/notify/store.py`
- Modify: `services/backend-api/app/routers/notify.py`
- Test: `tests/test_notify_center.py`

**Interfaces:**
- Produces: `delivery_receipt(conn, outbox_id: int, source_key: str) -> dict | None`
- Produces: `GET /v1/internal/notify/deliveries/{outbox_id}`

- [x] **Step 1: Write failing tests for source ownership and status counts**

```python
def test_delivery_receipt_is_scoped_to_authenticated_source(self):
    app = FastAPI()
    app.include_router(notify_router.router)
    app.dependency_overrides[notify_router._require_source] = lambda: "other-source"
    with mock.patch.object(notify_router, "_conn", return_value=FakeConnection(rows=[])):
        response = TestClient(app).get("/v1/internal/notify/deliveries/42")
    assert response.status_code == 404
```

- [x] **Step 2: Run the focused tests and confirm the route is absent**

Run: `.venv/bin/python -m pytest -q tests/test_notify_center.py -k delivery_receipt`

- [x] **Step 3: Implement a read-only receipt that excludes `target_json` and credentials**

```python
@router.get("/v1/internal/notify/deliveries/{outbox_id}")
def get_delivery_receipt(outbox_id: int, source_key: str = Depends(_require_source)) -> dict[str, Any]:
    with closing(_conn()) as conn:
        receipt = store.delivery_receipt(conn, outbox_id, source_key)
    if receipt is None:
        raise HTTPException(status_code=404, detail="notification delivery not found")
    return {"ok": True, **receipt}
```

- [x] **Step 4: Run notify-center tests**

Run: `.venv/bin/python -m pytest -q tests/test_notify_center.py`

### Task 3: Structured Replay Business Adapter

**Files:**
- Modify: `services/backend-api/app/routers/gold_spread_alerts.py`
- Test: `tests/test_backend_gold_spread_alerts.py`

**Interfaces:**
- Consumes: `dispatch.deliver()` enriched delivery result from Task 1
- Produces: optional `GoldSpreadAlert.replay_progress`
- Produces: structured `NotifySegment(kind="fields")` sections for progress, timing, and final report metrics

- [x] **Step 1: Write failing model, rendering, and response-receipt tests**

```python
def test_replay_summary_builds_structured_progress_segments():
    alert = alerts.GoldSpreadAlert.model_validate({
        "event_id": "remote-replay:job-123:succeeded",
        "kind": "replay_summary", "occurred_at": "2026-08-31T11:00:00+08:00",
        "source": "replay", "summary": "正式复盘完成",
        "replay_progress": {
            "job_id": "job-123", "status_code": "SUCCEEDED｜作业成功",
            "phase": "complete", "completed_partitions": 97,
            "total_partitions": 97, "worker_process_count": 0,
        },
    })
    notification = alerts.build_alert_notification(alert, alerts.render_gold_spread_alert(alert))
    fields = [segment for segment in notification.segments if segment.kind == "fields"]
    assert fields[0].fields[0].name == "作业"
```

- [x] **Step 2: Run the focused tests and confirm `replay_progress` is not represented**

Run: `.venv/bin/python -m pytest -q tests/test_backend_gold_spread_alerts.py -k replay`

- [x] **Step 3: Add typed replay progress and metric models, then build three message sections**

```python
class ReplayProgress(BaseModel):
    job_id: str
    status_code: str
    phase: str
    completed_partitions: int = Field(ge=0)
    total_partitions: int = Field(gt=0)
    worker_process_count: int = Field(ge=0)
    elapsed_seconds: int | None = Field(default=None, ge=0)
    estimated_remaining_seconds: int | None = Field(default=None, ge=0)
    report_metrics: list[ReplayMetric] = Field(default_factory=list)
```

- [x] **Step 4: Return the central outbox and delivery counts from the gold endpoint**

Expected response keys: `outbox_id`, `targets`, `sent`, `pending`, `dead`, `duplicate`.

- [x] **Step 5: Run gold-spread adapter tests**

Run: `.venv/bin/python -m pytest -q tests/test_backend_gold_spread_alerts.py`

### Task 4: Documentation and Full Verification

**Files:**
- Modify: `docs/runbooks/notify.md`
- Modify: `docs/project-ai-map.md`

**Interfaces:**
- Documents: domain-adapter boundary, duplicate receipt semantics, generic receipt query, end-to-end verification query.

- [x] **Step 1: Update the runbook with request/response and receipt examples using placeholders**

- [x] **Step 2: Run all backend tests**

Run: `.venv/bin/python -m pytest -q tests/test_backend_gold_spread_alerts.py tests/test_notify_center.py`

- [x] **Step 3: Run repository checks**

Run: `.venv/bin/python -m pytest -q tests`

Run: `.venv/bin/python scripts/check_navigation.py`

- [x] **Step 4: Verify no secrets and no unintended deployment or database changes**

Run: `git diff --check && git status --short`

- [x] **Step 5: Commit on the feature branch**

```bash
git add services/backend-api/app/notify services/backend-api/app/routers/notify.py services/backend-api/app/routers/gold_spread_alerts.py tests/test_notify_center.py tests/test_backend_gold_spread_alerts.py docs/runbooks/notify.md docs/project-ai-map.md docs/superpowers/plans/2026-08-31-gold-replay-notify-delivery.md
git commit -m "feat(notify): expose gold replay delivery evidence"
```
