import unittest
from typing import Any
from unittest.mock import patch

from tplus_datahub.jobs import db_sync_requests


class FakeCursor:
    def __init__(self):
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.rows = [
            (
                7,
                "incremental",
                {"parent_code": "HYD-4197PC"},
                "evt-1",
            )
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=()):
        self.statements.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def transaction(self):
        return FakeTransaction()


class DbSyncRequestTests(unittest.TestCase):
    def test_fetch_next_bom_request_marks_request_running(self):
        conn = FakeConn()

        request = db_sync_requests.fetch_next_bom_request(conn)

        self.assertEqual(7, request["id"])
        self.assertEqual("incremental", request["mode"])
        self.assertEqual({"parent_code": "HYD-4197PC"}, request["target_json"])
        joined_sql = "\n".join(statement for statement, _ in conn.cursor_obj.statements)
        self.assertIn("FOR UPDATE SKIP LOCKED", joined_sql)
        self.assertIn("UPDATE integration_sync_requests", joined_sql)

    def test_fetch_next_full_request_only_picks_the_all_module(self):
        """全量请求和 BOM 回写请求共用一张表，靠 module 分流——取错会把回写请求当全量跑。"""
        conn = FakeConn()

        request = db_sync_requests.fetch_next_full_request(conn)

        self.assertEqual(7, request["id"])
        joined_sql = "\n".join(statement for statement, _ in conn.cursor_obj.statements)
        self.assertIn("module = 'all'", joined_sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", joined_sql)
        self.assertIn("UPDATE integration_sync_requests", joined_sql)

    def test_fetch_next_bom_request_never_picks_up_full_requests(self):
        joined_sql = db_sync_requests._NEXT_BOM_REQUEST_SQL
        self.assertIn("module = 'bom'", joined_sql)
        self.assertNotIn("module = 'all'", joined_sql)


class FinishFullRequestTests(unittest.TestCase):
    def test_manual_full_run_is_not_recorded_as_scheduled_full(self):
        """记成 scheduled_full 会顶掉锚点相位：fetch_last_scheduled_full_at() 会把手动这次
        当成"今天已经跑过"，当晚的定时轮次直接判定未到期被整轮跳过。"""
        sql = db_sync_requests._RECORD_FULL_RUN_SQL
        self.assertIn("'manual_full'", sql)
        self.assertNotIn("scheduled_full", sql)
        self.assertIn("'all'", sql)

    def test_manual_full_commits_before_attaching_platform_run(self):
        events = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, sql, _params=()):
                events.append(sql)

            def fetchone(self):
                return (55,)

        class Conn:
            def cursor(self):
                return Cursor()

            def commit(self):
                events.append("commit")

            def close(self):
                events.append("close")

        with (
            patch.object(db_sync_requests, "connect_if_configured", return_value=Conn()),
            patch.object(
                db_sync_requests,
                "attach_legacy_ref",
                side_effect=lambda platform_id, legacy_id: events.append(("attach", platform_id, legacy_id)),
                create=True,
            ),
        ):
            db_sync_requests.finish_full_request(9, "success", 0, {"platform_run_id": 77})

        joined_sql = "\n".join(item for item in events if isinstance(item, str))
        self.assertIn("'manual_full'", joined_sql)
        self.assertLess(events.index("commit"), events.index(("attach", 77, 55)))

    def test_manual_full_attach_failure_does_not_change_committed_legacy_result(self):
        events = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, _sql, _params=()):
                events.append("execute")

            def fetchone(self):
                return (55,)

        class Conn:
            def cursor(self):
                return Cursor()

            def commit(self):
                events.append("commit")

            def close(self):
                events.append("close")

        with (
            patch.object(db_sync_requests, "connect_if_configured", return_value=Conn()),
            patch.object(
                db_sync_requests,
                "attach_legacy_ref",
                side_effect=RuntimeError("platform down"),
                create=True,
            ) as attach,
        ):
            db_sync_requests.finish_full_request(9, "success", 0, {"platform_run_id": 77})

        self.assertIn("commit", events)
        attach.assert_called_once_with(77, 55)

    def test_manual_full_commit_failure_never_attaches_platform_run(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, _sql, _params=()):
                return None

            def fetchone(self):
                return (55,)

        class Conn:
            def cursor(self):
                return Cursor()

            def commit(self):
                raise RuntimeError("commit failed")

            def close(self):
                return None

        with (
            patch.object(db_sync_requests, "connect_if_configured", return_value=Conn()),
            patch.object(db_sync_requests, "attach_legacy_ref", create=True) as attach,
        ):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                db_sync_requests.finish_full_request(9, "success", 0, {"platform_run_id": 77})

        attach.assert_not_called()


if __name__ == "__main__":
    unittest.main()
