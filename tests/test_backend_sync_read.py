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


if __name__ == "__main__":
    unittest.main()
