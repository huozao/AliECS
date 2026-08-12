from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))

try:
    from app import sync_read
except ImportError:
    sync_read = None


class SyncReadTestCase(unittest.TestCase):
    def setUp(self):
        if sync_read is None:
            self.fail("app.sync_read is not implemented")


class FreshnessTests(SyncReadTestCase):
    def test_null_sla_is_unmonitored(self):
        value = sync_read.classify_freshness(
            datetime(2026, 8, 12, tzinfo=timezone.utc),
            None,
            now=datetime(2026, 8, 13, tzinfo=timezone.utc),
        )

        self.assertEqual(
            {
                "state": "unmonitored",
                "sla_seconds": None,
                "age_seconds": None,
                "ratio": None,
            },
            value,
        )

    def test_never_run_is_distinct_from_stale(self):
        value = sync_read.classify_freshness(
            None,
            3600,
            now=datetime(2026, 8, 13, tzinfo=timezone.utc),
        )

        self.assertEqual("never", value["state"])

    def test_warning_starts_at_eighty_percent(self):
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        value = sync_read.classify_freshness(
            now - timedelta(seconds=2880),
            3600,
            now=now,
        )

        self.assertEqual("warning", value["state"])

    def test_stale_is_strictly_past_sla(self):
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)

        self.assertEqual(
            "fresh",
            sync_read.classify_freshness(
                now - timedelta(seconds=2879), 3600, now=now
            )["state"],
        )
        self.assertEqual(
            "warning",
            sync_read.classify_freshness(
                now - timedelta(seconds=3600), 3600, now=now
            )["state"],
        )
        self.assertEqual(
            "stale",
            sync_read.classify_freshness(
                now - timedelta(seconds=3601), 3600, now=now
            )["state"],
        )

    def test_subsecond_boundaries_use_exact_elapsed_time(self):
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)

        for elapsed, expected in (
            (2879.999, "fresh"),
            (2880.001, "warning"),
            (3600.001, "stale"),
        ):
            with self.subTest(elapsed=elapsed):
                value = sync_read.classify_freshness(
                    now - timedelta(seconds=elapsed),
                    3600,
                    now=now,
                )
                self.assertEqual(expected, value["state"])
                self.assertEqual(int(elapsed), value["age_seconds"])
                self.assertAlmostEqual(elapsed / 3600, value["ratio"], places=9)


class ErrorKindLabelTests(SyncReadTestCase):
    def test_known_error_kinds_have_fixed_labels(self):
        expected = {
            "auth": "凭据过期",
            "rate_limit": "请求限流",
            "network": "网络异常",
            "schema": "数据结构变化",
            "write": "写入失败",
            "unknown": "未知错误",
        }

        self.assertEqual(
            expected,
            {kind: sync_read.error_kind_label(kind) for kind in expected},
        )

    def test_missing_or_unrecognized_error_kind_is_unknown(self):
        self.assertEqual("未知错误", sync_read.error_kind_label(None))
        self.assertEqual("未知错误", sync_read.error_kind_label("timeout"))


class FormulaArtifactTests(SyncReadTestCase):
    def test_reports_exact_file_selected_by_formula(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "RECIPE_BOM_INPUT_DIR": tmp,
                "RECIPE_BOM_INPUT_PATH": "",
                "RECIPE_BOM_INPUT_GLOB": "*bom*.xlsx",
            },
        ):
            older = Path(tmp) / "bom_20260811_020000.xlsx"
            older.write_bytes(b"older")
            os.utime(older, (1_786_390_000, 1_786_390_000))
            selected = Path(tmp) / "bom_20260812_020000.xlsx"
            selected.write_bytes(b"test")
            os.utime(selected, (1_786_476_000, 1_786_476_000))

            artifact = sync_read.formula_bom_artifact()

            self.assertEqual(selected.name, artifact["name"])
            self.assertEqual(int(selected.stat().st_mtime), artifact["mtime_epoch"])
            self.assertEqual(
                datetime.fromtimestamp(
                    selected.stat().st_mtime, timezone.utc
                ).isoformat(),
                artifact["mtime"],
            )

    def test_missing_formula_input_returns_none_without_creating_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "RECIPE_BOM_INPUT_DIR": tmp,
                "RECIPE_BOM_INPUT_PATH": "",
                "RECIPE_BOM_INPUT_GLOB": "*bom*.xlsx",
            },
        ):
            self.assertIsNone(sync_read.formula_bom_artifact())
            self.assertEqual([], list(Path(tmp).iterdir()))


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
FORMULA_ARTIFACT = {
    "name": "bom_20260813_020000.xlsx",
    "mtime": "2026-08-13T02:00:00+00:00",
    "mtime_epoch": 1_786_586_400,
}


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.connection.queries.append((sql, tuple(params or ())))
        if not self.connection.responses:
            raise AssertionError(f"unexpected SQL: {sql}")
        self.rows = self.connection.responses.pop(0)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class FakeConnection:
    def __init__(self, *responses: list[tuple[Any, ...]]) -> None:
        self.responses = list(responses)
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def joined_sql(self) -> str:
        return "\n".join(sql for sql, _ in self.queries)


def run_row(
    *,
    run_id: int = 91,
    job_key: str = "wecom.doc.17",
    provider: str = "wecom",
    status: str = "failed",
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    error_kind: str | None = "rate_limit",
    detail_json: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    return (
        run_id,
        job_key,
        "同步作业",
        provider,
        "pull",
        "schedule",
        status,
        started_at if started_at is not None else NOW - timedelta(minutes=5),
        finished_at,
        120,
        12,
        error_kind,
        "sanitized failure" if error_kind else None,
        detail_json or {},
        {"table": "sync_runs", "id": 7},
    )


def overview_row(
    *,
    job_key: str,
    provider: str,
    display_name: str,
    status: str | None,
    last_success_at: datetime | None,
    sla_seconds: int | None,
    error_kind: str | None = None,
    open_alert_count: int = 0,
) -> tuple[Any, ...]:
    has_run = status is not None
    return (
        job_key,
        "pull",
        provider,
        display_name,
        True,
        {},
        sla_seconds,
        None,
        True,
        91 if has_run else None,
        "schedule" if has_run else None,
        status,
        NOW - timedelta(minutes=5) if has_run else None,
        NOW - timedelta(minutes=4) if has_run and status != "running" else None,
        120 if has_run else None,
        12 if has_run else None,
        error_kind,
        "sanitized failure" if error_kind else None,
        {"phase": "done"} if has_run else None,
        {"table": "sync_runs", "id": 7} if has_run else None,
        last_success_at,
        open_alert_count,
    )


class OverviewReadTests(SyncReadTestCase):
    def setUp(self) -> None:
        super().setUp()
        if not hasattr(sync_read, "overview"):
            self.fail("sync_read.overview is not implemented")

    def test_empty_database_has_zeroed_summary(self):
        result = sync_read.overview(FakeConnection([]), now=NOW)

        self.assertEqual([], result["items"])
        self.assertEqual(
            {
                "jobs": 0,
                "fresh": 0,
                "warning": 0,
                "stale": 0,
                "never": 0,
                "unmonitored": 0,
                "failed": 0,
                "partial": 0,
                "running": 0,
                "open_alerts": 0,
            },
            result["summary"],
        )

    def test_overview_shape_and_summary(self):
        rows = [
            overview_row(
                job_key="wecom.doc.17",
                provider="wecom",
                display_name="企微文档",
                status="failed",
                last_success_at=NOW - timedelta(seconds=1_000),
                sla_seconds=3_600,
                error_kind="auth",
                open_alert_count=2,
            ),
            overview_row(
                job_key="feishu.doc.23",
                provider="feishu",
                display_name="飞书文档",
                status="running",
                last_success_at=NOW - timedelta(seconds=3_000),
                sla_seconds=3_600,
            ),
            overview_row(
                job_key="chanjet.full",
                provider="chanjet",
                display_name="T+ 全量同步",
                status="partial",
                last_success_at=NOW - timedelta(seconds=4_000),
                sla_seconds=3_600,
            ),
            overview_row(
                job_key="tplus.parent_match",
                provider="chanjet",
                display_name="父件核对",
                status="success",
                last_success_at=NOW - timedelta(seconds=120),
                sla_seconds=None,
            ),
        ]
        conn = FakeConnection(rows)

        with patch.object(
            sync_read, "formula_bom_artifact", return_value=FORMULA_ARTIFACT
        ):
            result = sync_read.overview(conn, now=NOW)

        self.assertEqual(
            {
                "jobs": 4,
                "fresh": 1,
                "warning": 1,
                "stale": 1,
                "never": 0,
                "unmonitored": 1,
                "failed": 1,
                "partial": 1,
                "running": 1,
                "open_alerts": 2,
            },
            result["summary"],
        )
        self.assertEqual(4, len(result["items"]))
        self.assertEqual("凭据过期", result["items"][0]["last_run"]["error_label"])
        self.assertEqual({}, result["items"][0]["schedule"])
        self.assertIsNone(result["items"][0]["next_expected_at"])
        self.assertEqual("unmonitored", result["items"][3]["freshness"]["state"])
        self.assertIsNone(result["items"][0]["artifact"])
        self.assertEqual(FORMULA_ARTIFACT, result["items"][2]["artifact"])
        self.assertIsNone(result["items"][3]["artifact"])

    def test_overview_sql_uses_lateral_latest_rows_and_preaggregated_alerts(self):
        conn = FakeConnection([])

        sync_read.overview(conn, now=NOW)

        sql = conn.joined_sql()
        self.assertIn("FROM sync_jobs j", sql)
        self.assertGreaterEqual(sql.count("LEFT JOIN LATERAL"), 2)
        self.assertIn("status = 'success'", sql)
        self.assertIn("state = 'open'", sql)
        self.assertIn("GROUP BY job_id", sql)
        self.assertNotIn("external_doc_id", sql)
        self.assertNotIn("alert_chat_id", sql)


class AlertReadTests(SyncReadTestCase):
    def setUp(self) -> None:
        super().setUp()
        if not hasattr(sync_read, "alerts_page"):
            self.fail("sync_read.alerts_page is not implemented")

    def test_open_alerts_use_count_and_page_queries_with_parameters(self):
        alert = (
            401,
            "wecom.doc.17",
            "企微文档",
            "wecom",
            91,
            "failed",
            "open",
            NOW - timedelta(hours=2),
            NOW - timedelta(hours=1),
            2,
            None,
            {"status": "failed"},
        )
        conn = FakeConnection([(1,)], [alert])

        result = sync_read.alerts_page(
            conn, state="open", limit=50, offset=0
        )

        self.assertEqual(1, result["total"])
        self.assertEqual("wecom.doc.17", result["items"][0]["job_key"])
        self.assertEqual("open", result["items"][0]["state"])
        self.assertEqual(2, len(conn.queries))
        self.assertIn("a.state = %s", conn.queries[0][0])
        self.assertEqual(("open",), conn.queries[0][1])
        self.assertEqual(("open", 50, 0), conn.queries[1][1])
        self.assertNotIn("external_doc_id", conn.joined_sql())
        self.assertNotIn("alert_chat_id", conn.joined_sql())

    def test_all_alerts_omit_state_predicate_but_keep_paging_parameterized(self):
        conn = FakeConnection([(0,)], [])

        result = sync_read.alerts_page(
            conn, state="all", limit=25, offset=75
        )

        self.assertEqual({"items": [], "total": 0, "limit": 25, "offset": 75}, result)
        self.assertNotIn("a.state = %s", conn.joined_sql())
        self.assertEqual((), conn.queries[0][1])
        self.assertEqual((25, 75), conn.queries[1][1])


class RunTimelineReadTests(SyncReadTestCase):
    def setUp(self) -> None:
        super().setUp()
        if not hasattr(sync_read, "runs_page"):
            self.fail("sync_read.runs_page is not implemented")

    def test_global_filters_and_paging_are_parameterized_with_one_predicate(self):
        hostile_job = "wecom.doc.17' OR TRUE --"
        conn = FakeConnection([(87,)], [run_row()])

        page = sync_read.runs_page(
            conn,
            job_key=hostile_job,
            provider="wecom",
            status="failed",
            limit=20,
            offset=40,
            now=NOW,
        )

        self.assertEqual(87, page["total"])
        self.assertEqual(91, page["items"][0]["id"])
        sql = conn.joined_sql()
        for predicate in ("j.job_key = %s", "j.provider = %s", "r.status = %s"):
            self.assertEqual(2, sql.count(predicate))
        for value in (hostile_job, "wecom", "failed"):
            self.assertNotIn(value, sql)
        expected_filters = (hostile_job, "wecom", "failed")
        self.assertEqual(expected_filters, conn.queries[0][1])
        self.assertEqual((*expected_filters, 20, 40), conn.queries[1][1])

    def test_runs_have_stable_global_order_and_complete_read_shape(self):
        conn = FakeConnection([(1,)], [run_row(finished_at=NOW)])

        page = sync_read.runs_page(
            conn,
            job_key=None,
            provider=None,
            status=None,
            limit=20,
            offset=0,
            now=NOW,
        )

        self.assertIn("ORDER BY r.started_at DESC, r.id DESC", conn.joined_sql())
        self.assertEqual(
            {
                "id",
                "job_key",
                "display_name",
                "provider",
                "kind",
                "trigger",
                "status",
                "started_at",
                "finished_at",
                "row_count",
                "changed_count",
                "error_kind",
                "error_label",
                "error_message",
                "detail_json",
                "legacy_ref",
                "duration_seconds",
            },
            set(page["items"][0]),
        )
        self.assertEqual("请求限流", page["items"][0]["error_label"])
        self.assertEqual(300.0, page["items"][0]["duration_seconds"])

    def test_duration_handles_running_missing_finish_and_negative_clock(self):
        rows = [
            run_row(
                run_id=1,
                status="running",
                started_at=NOW - timedelta(seconds=5),
                finished_at=None,
                error_kind=None,
            ),
            run_row(
                run_id=2,
                status="failed",
                started_at=NOW - timedelta(seconds=5),
                finished_at=None,
            ),
            run_row(
                run_id=3,
                status="success",
                started_at=NOW,
                finished_at=NOW - timedelta(seconds=5),
                error_kind=None,
            ),
        ]
        conn = FakeConnection([(3,)], rows)

        page = sync_read.runs_page(
            conn,
            job_key=None,
            provider=None,
            status=None,
            limit=20,
            offset=0,
            now=NOW,
        )

        self.assertEqual(
            [5.0, None, 0.0],
            [item["duration_seconds"] for item in page["items"]],
        )

    def test_job_existence_query_uses_job_key_as_a_value(self):
        hostile_job = "missing' OR TRUE --"
        conn = FakeConnection([])

        self.assertFalse(sync_read.job_exists(conn, hostile_job))

        self.assertIn("WHERE job_key = %s", conn.queries[0][0])
        self.assertNotIn(hostile_job, conn.queries[0][0])
        self.assertEqual((hostile_job,), conn.queries[0][1])


class RunDetailReadTests(SyncReadTestCase):
    def setUp(self) -> None:
        super().setUp()
        if not hasattr(sync_read, "run_detail"):
            self.fail("sync_read.run_detail is not implemented")

    def test_run_detail_orders_steps_labels_error_and_computes_durations(self):
        steps = [
            (1, "token", "success", NOW - timedelta(seconds=4), NOW - timedelta(seconds=3), 0, None),
            (2, "fetch", "running", NOW - timedelta(seconds=2), None, 40, None),
            (3, "write", "failed", NOW, NOW - timedelta(seconds=1), 0, "sanitized step failure"),
        ]
        conn = FakeConnection(
            [run_row(finished_at=NOW)],
            steps,
        )

        detail = sync_read.run_detail(conn, 91, now=NOW)

        self.assertEqual([1, 2, 3], [step["seq"] for step in detail["steps"]])
        self.assertIn("ORDER BY seq ASC", conn.queries[1][0])
        self.assertEqual("请求限流", detail["run"]["error_label"])
        self.assertEqual([1.0, 2.0, 0.0], [step["duration_seconds"] for step in detail["steps"]])
        self.assertEqual("sanitized step failure", detail["steps"][2]["message"])
        self.assertIsNone(detail["reconciliation_id"])
        self.assertNotIn("integration_reconciliation_diffs", conn.joined_sql())

    def test_chanjet_full_uses_snapshot_id_for_bom_reconciliation_only(self):
        conn = FakeConnection(
            [
                run_row(
                    job_key="chanjet.full",
                    provider="chanjet",
                    status="success",
                    finished_at=NOW,
                    error_kind=None,
                    detail_json={"full_snapshot_id": 812},
                )
            ],
            [],
            [(777,)],
        )

        detail = sync_read.run_detail(conn, 91, now=NOW)

        self.assertEqual(777, detail["reconciliation_id"])
        reconciliation_sql, params = conn.queries[2]
        self.assertIn("FROM integration_reconciliation_diffs", reconciliation_sql)
        self.assertIn("provider = 'chanjet'", reconciliation_sql)
        self.assertIn("module = 'bom'", reconciliation_sql)
        self.assertIn("full_snapshot_id = %s", reconciliation_sql)
        self.assertIn("ORDER BY created_at DESC, id DESC", reconciliation_sql)
        self.assertEqual((812,), params)

    def test_chanjet_full_without_matching_diff_returns_null(self):
        conn = FakeConnection(
            [
                run_row(
                    job_key="chanjet.full",
                    provider="chanjet",
                    status="success",
                    finished_at=NOW,
                    error_kind=None,
                    detail_json={"full_snapshot_id": 812},
                )
            ],
            [],
            [],
        )

        detail = sync_read.run_detail(conn, 91, now=NOW)

        self.assertIsNone(detail["reconciliation_id"])

    def test_missing_run_returns_none_without_followup_queries(self):
        conn = FakeConnection([])

        self.assertIsNone(sync_read.run_detail(conn, 404, now=NOW))
        self.assertEqual(1, len(conn.queries))


if __name__ == "__main__":
    unittest.main()
