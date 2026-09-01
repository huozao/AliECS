from __future__ import annotations

import contextlib
import io
import sys
import unittest
from unittest.mock import patch
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"
sys.path.insert(0, str(WORKER_ROOT))


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


class WorkerImportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        _clear_app_modules()
        worker_root = str(WORKER_ROOT)
        sys.path[:] = [item for item in sys.path if item != worker_root]
        sys.path.insert(0, worker_root)

    def tearDown(self) -> None:
        _clear_app_modules()
        sys.path[:] = self._old_sys_path


class WeComSmartsheetPaginationTests(WorkerImportTestCase):
    def test_get_records_stops_after_single_page_when_has_more_false(self) -> None:
        from app.providers.wecom import WeComSmartsheetClient

        class FakeClient(WeComSmartsheetClient):
            def __init__(self) -> None:
                super().__init__("corp", "secret")
                self.calls: list[dict] = []

            def _post(self, path: str, payload: dict) -> dict:
                self.calls.append({"path": path, "payload": payload})
                return {
                    "errcode": 0,
                    "has_more": False,
                    "next": "",
                    "records": [{"record_id": "r1"}],
                }

        client = FakeClient()

        result = client.get_records("doc1", "sheet1")

        self.assertEqual(1, result["fetched_count"])
        self.assertEqual(1, result["page_count"])
        self.assertEqual([{"record_id": "r1"}], result["records"])
        self.assertEqual(1, len(client.calls))
        self.assertNotIn("next", client.calls[0]["payload"])

    @staticmethod
    def _offset_only_client(total: int, unreadable_positions: frozenset[int] = frozenset()):
        """模拟企微真实行为：只认请求里的 offset，回传的 next 一律忽略。

        2026-08-28 实测：连发 8 次带 next 的请求全部拿回同一页、has_more 恒为 true。
        旧实现拿 next 当游标翻页，遇到多页就是死循环——这个假客户端专门锁住这条契约。
        unreadable_positions 里的 1-based 序号模拟成员字段指向失效 userid 的记录：
        请求窗口一旦覆盖到它就整体报 60111。
        """
        from app.providers.wecom import WeComApiError, WeComSmartsheetClient

        class FakeClient(WeComSmartsheetClient):
            def __init__(self) -> None:
                super().__init__("corp", "secret")
                self.calls: list[dict] = []

            def _post(self, path: str, payload: dict) -> dict:
                self.calls.append({"path": path, "payload": dict(payload)})
                offset = int(payload.get("offset") or 0)
                limit = int(payload.get("limit") or total)
                window = range(offset + 1, min(offset + limit, total) + 1)
                if unreadable_positions & set(window):
                    raise WeComApiError(path, {"errcode": 60111, "errmsg": "userid not found"})
                records = [{"record_id": f"r{index}"} for index in window]
                return {
                    "errcode": 0,
                    "total": total,
                    "has_more": offset + len(records) < total,
                    "next": offset + len(records),
                    "records": records,
                }

        return FakeClient()

    def test_get_records_pages_by_offset_and_never_sends_next(self) -> None:
        client = self._offset_only_client(total=120)

        result = client.get_records("doc1", "sheet1")

        self.assertEqual(120, result["fetched_count"])
        self.assertEqual(3, result["page_count"])
        self.assertEqual(0, result["unreadable_count"])
        self.assertEqual(
            [f"r{index}" for index in range(1, 121)],
            [item["record_id"] for item in result["records"]],
        )
        self.assertEqual([0, 50, 100], [int(call["payload"].get("offset") or 0) for call in client.calls])
        for call in client.calls:
            self.assertNotIn("next", call["payload"])

    def test_get_records_skips_records_wecom_refuses_to_return(self) -> None:
        client = self._offset_only_client(total=146, unreadable_positions=frozenset({85, 86, 97}))

        result = client.get_records("doc1", "sheet1")

        self.assertEqual([85, 86, 97], result["unreadable_offsets"])
        self.assertEqual(3, result["unreadable_count"])
        self.assertEqual(143, result["fetched_count"])
        self.assertNotIn("r85", [item["record_id"] for item in result["records"]])
        self.assertIn("r84", [item["record_id"] for item in result["records"]])
        self.assertIn("r146", [item["record_id"] for item in result["records"]])

    def test_get_records_propagates_errors_that_are_not_unreadable_records(self) -> None:
        from app.providers.wecom import WeComApiError, WeComSmartsheetClient

        class FakeClient(WeComSmartsheetClient):
            def __init__(self) -> None:
                super().__init__("corp", "secret")

            def _post(self, path: str, payload: dict) -> dict:
                raise WeComApiError(path, {"errcode": 42001, "errmsg": "access_token expired"})

        with self.assertRaises(WeComApiError):
            FakeClient().get_records("doc1", "sheet1")


class SyncJobRunRetentionTests(WorkerImportTestCase):
    """运行记录清理：跳过留痕之后写入量约 90 行/天，必须有保留期。"""

    def test_prune_passes_retention_window_and_per_job_floor(self) -> None:
        from app.pipelines import worker_loop

        class FakeStore:
            def __init__(self) -> None:
                self.calls: list[tuple[int, int]] = []
                self.closed = 0

            def prune_sync_job_runs(self, retain_days: int, min_runs_per_job: int) -> int:
                self.calls.append((retain_days, min_runs_per_job))
                return 12

            def close(self) -> None:
                self.closed += 1

        store = FakeStore()
        with patch.object(worker_loop, "open_store", return_value=store):
            deleted = worker_loop.prune_sync_job_runs()

        self.assertEqual(12, deleted)
        self.assertEqual([(worker_loop.RUN_RETENTION_DAYS, worker_loop.RUN_RETENTION_MIN_PER_JOB)], store.calls)
        self.assertEqual(1, store.closed)
        # 保底条数不能省：纯按时间删会把低频作业删成「无记录」。
        self.assertGreaterEqual(worker_loop.RUN_RETENTION_MIN_PER_JOB, 1)

    def test_prune_is_a_noop_on_stores_without_the_method(self) -> None:
        from app.pipelines import worker_loop

        class OldStore:
            def __init__(self) -> None:
                self.closed = 0

            def close(self) -> None:
                self.closed += 1

        store = OldStore()
        with patch.object(worker_loop, "open_store", return_value=store):
            self.assertEqual(0, worker_loop.prune_sync_job_runs())
        self.assertEqual(1, store.closed)


class SourcelessJobMetadataTests(WorkerImportTestCase):
    def test_locator_mirror_is_accepted_as_a_sourceless_job(self) -> None:
        from app.storage.sync_job_platform import SyncJobPlatformWriter

        SyncJobPlatformWriter._validate_start("wecom.locator_mirror", "mirror", "wecom", None, {})

    def test_locator_mirror_rejects_a_source_id_or_wrong_kind(self) -> None:
        from app.storage.sync_job_platform import SyncJobPlatformWriter

        with self.assertRaises(ValueError):
            SyncJobPlatformWriter._validate_start("wecom.locator_mirror", "mirror", "wecom", 7, {})
        with self.assertRaises(ValueError):
            SyncJobPlatformWriter._validate_start("wecom.locator_mirror", "pull", "wecom", None, {})

    def test_skipped_is_a_valid_run_status(self) -> None:
        from app.storage.sync_job_platform import _RUN_STATUSES

        self.assertIn("skipped", _RUN_STATUSES)


class ExternalRecordHashTests(WorkerImportTestCase):
    def test_same_record_hash_is_unchanged(self) -> None:
        from app.storage.postgres import build_record_snapshot, decide_record_upsert

        raw_record = {"record_id": "r1", "values": {"f1": [{"text": "alpha"}]}}
        snapshot = build_record_snapshot(raw_record, {"f1": "字段一"})

        decision = decide_record_upsert(snapshot.record_hash, snapshot)

        self.assertEqual("unchanged", decision.action)
        self.assertFalse(decision.should_write)

    def test_changed_record_hash_requests_update(self) -> None:
        from app.storage.postgres import build_record_snapshot, decide_record_upsert

        old_snapshot = build_record_snapshot({"record_id": "r1", "values": {"f1": [{"text": "alpha"}]}}, {"f1": "字段一"})
        new_snapshot = build_record_snapshot({"record_id": "r1", "values": {"f1": [{"text": "beta"}]}}, {"f1": "字段一"})

        decision = decide_record_upsert(old_snapshot.record_hash, new_snapshot)

        self.assertEqual("update", decision.action)
        self.assertTrue(decision.should_write)

    def test_upsert_record_updates_when_normalized_json_changes_without_raw_hash_change(self) -> None:
        from app.storage.postgres import PostgresDocSyncStore, build_record_snapshot

        class FakeCursor:
            def __init__(self) -> None:
                self.executed: list[str] = []

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def execute(self, sql: str, params=None) -> None:
                self.executed.append(sql)

            def fetchone(self):
                return (snapshot.record_hash, {"附件": "1个附件"})

        class FakeConn:
            def __init__(self) -> None:
                self.cursor_obj = FakeCursor()
                self.commits = 0

            def cursor(self):
                return self.cursor_obj

            def commit(self) -> None:
                self.commits += 1

        snapshot = build_record_snapshot(
            {"record_id": "r1", "values": {"f1": [{"text": "1个附件", "link": "https://example.com/detail"}]}},
            {"f1": "附件"},
        )
        conn = FakeConn()

        decision = PostgresDocSyncStore(conn).upsert_record(10, snapshot)

        self.assertEqual("update", decision.action)
        self.assertTrue(decision.should_write)
        self.assertTrue(any("UPDATE external_records" in sql for sql in conn.cursor_obj.executed))
        self.assertEqual(1, conn.commits)


class SourceUrlTests(WorkerImportTestCase):
    def test_build_smartsheet_open_url_prefers_source_url(self) -> None:
        from app.storage.postgres import build_smartsheet_open_url

        url = build_smartsheet_open_url("dcabc", "sheet1", "https://doc.weixin.qq.com/smartsheet/dcabc?tab=sheet1")

        self.assertEqual("https://doc.weixin.qq.com/smartsheet/dcabc?tab=sheet1", url)

    def test_build_smartsheet_open_url_falls_back_to_docid_and_sheet_id(self) -> None:
        from app.storage.postgres import build_smartsheet_open_url

        url = build_smartsheet_open_url("dcabc", "sheet1", "")

        self.assertEqual("https://doc.weixin.qq.com/smartsheet/dcabc?sheet_id=sheet1", url)


class ImageCellTests(WorkerImportTestCase):
    def test_image_field_normalizes_to_urls(self) -> None:
        from app.storage.postgres import build_record_snapshot

        record = {
            "record_id": "r1",
            "values": {
                "f1": [{"text": "烤全猪", "type": "text"}],
                "f2": [
                    {"id": "img-1", "title": "image/png", "image_url": "https://wdcdn.qpic.cn/a1?w=1"},
                    {"id": "img-2", "title": "image/jpeg", "image_url": "https://wdcdn.qpic.cn/a2?w=1"},
                ],
            },
        }
        snapshot = build_record_snapshot(record, {"f1": "菜品", "f2": "菜品参考图"})

        self.assertEqual("烤全猪", snapshot.normalized_json["菜品"])
        self.assertEqual(
            "https://wdcdn.qpic.cn/a1?w=1; https://wdcdn.qpic.cn/a2?w=1",
            snapshot.normalized_json["菜品参考图"],
        )

    def test_text_cell_behavior_unchanged_for_link_fields(self) -> None:
        from app.storage.postgres import first_text_cell

        self.assertEqual("官网 <https://example.com>", first_text_cell([{"text": "官网", "url": "https://example.com"}]))
        self.assertEqual("", first_text_cell([]))
        self.assertEqual("plain", first_text_cell("plain"))

    def test_url_cell_preserves_display_text_and_real_link(self) -> None:
        from app.storage.postgres import build_record_snapshot, first_text_cell

        cell = [{"link": "https://example.com/detail?sp_no=202603240010", "text": "1个附件", "type": "url"}]
        snapshot = build_record_snapshot({"record_id": "r1", "values": {"f1": cell}}, {"f1": "附件"})

        self.assertEqual("1个附件 <https://example.com/detail?sp_no=202603240010>", first_text_cell(cell))
        self.assertEqual("1个附件 <https://example.com/detail?sp_no=202603240010>", snapshot.normalized_json["附件"])

    def test_url_cell_with_matching_text_and_link_returns_link_once(self) -> None:
        from app.storage.postgres import first_text_cell

        self.assertEqual("https://example.com/a", first_text_cell([{"link": "https://example.com/a", "text": "https://example.com/a", "type": "url"}]))


class WorkerLoopTests(WorkerImportTestCase):
    @staticmethod
    def _one_minute_schedule() -> dict:
        return {"enabled": True, "interval_seconds": 60, "anchor_time": "", "pull_paused": False}

    @staticmethod
    def _read_and_tick(clock: dict) -> object:
        from datetime import timedelta

        observed = clock["now"]
        clock["now"] = observed + timedelta(microseconds=1)
        return observed

    def test_platform_catalog_reconciler_owns_store_lifecycle(self) -> None:
        from app.pipelines import worker_loop as module

        events: list[object] = []

        class Jobs:
            def reconcile_document_jobs(self) -> dict[str, int]:
                events.append("reconcile")
                return {"enabled": 2, "disabled": 0}

        class Store:
            sync_jobs = Jobs()

            def close(self) -> None:
                events.append("close")

        with patch.object(module, "open_store", side_effect=lambda: events.append("open") or Store()):
            module._reconcile_platform_catalog()

        self.assertEqual(["open", "reconcile", "close"], events)

    def test_platform_catalog_reconciler_is_fail_open_and_closes_store(self) -> None:
        from app.pipelines import worker_loop as module

        events: list[str] = []

        class Jobs:
            def reconcile_document_jobs(self) -> None:
                events.append("reconcile")
                raise RuntimeError("catalog unavailable")

        class Store:
            sync_jobs = Jobs()

            def close(self) -> None:
                events.append("close")

        with patch.object(module, "open_store", return_value=Store()):
            module._reconcile_platform_catalog()

        self.assertEqual(["reconcile", "close"], events)

    def test_injected_worker_loop_never_opens_platform_catalog(self) -> None:
        from datetime import datetime, timezone
        from app.pipelines import worker_loop as module

        with patch.object(
            module,
            "_reconcile_platform_catalog",
            side_effect=AssertionError("injected loop must not touch database"),
        ):
            code = module.run_worker_loop(
                full_sync=lambda: 0,
                consume_requests=lambda: 0,
                notifier_once=lambda: {},
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=lambda: {
                    "enabled": False,
                    "interval_seconds": 60,
                    "anchor_time": "",
                    "pull_paused": False,
                },
                now_fn=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
            )

        self.assertEqual(0, code)

    def test_explicit_notifier_runs_before_full_and_after_each_pending_poll(self) -> None:
        import unittest.mock as mock

        from app.pipelines.worker_loop import run_worker_loop

        events: list[str] = []
        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}):
            code = run_worker_loop(
                full_sync=lambda: events.append("full") or 0,
                consume_requests=lambda: events.append("pending") or 0,
                notifier_once=lambda: events.append("notifier") or {},
                sleep=lambda _seconds: events.append("sleep"),
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: events.append("config") or "noop",
                last_full_reader=lambda: None,
            )

        self.assertEqual(0, code)
        self.assertEqual(
            [
                "notifier", "full",
                "sleep", "pending", "notifier", "config",
                "sleep", "pending", "notifier",
            ],
            events,
        )

    def test_notify_flush_runs_once_per_poll_not_once_per_cycle(self) -> None:
        """通知冲刷必须跟着 poll 走，不能跟着外层 cycle 走。

        外层 while True 的一轮 = 一个完整调度周期（interval_seconds 默认 86400），
        把 flush 放在那一层等于一天才冲刷一次。2026-08-31 上线自检实测到这个：
        worker 写进 outbox 的通知 120 秒后仍未被带走。判据必须是「每次 poll 一次」，
        否则同样的错误换个写法还会再犯。
        """
        import unittest.mock as mock

        from app.pipelines import worker_loop as module

        events: list[str] = []
        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), mock.patch.object(
            module.notify_client, "request_flush", lambda: events.append("flush")
        ):
            code = module.run_worker_loop(
                full_sync=lambda: events.append("full") or 0,
                consume_requests=lambda: events.append("pending") or 0,
                notifier_once=lambda: {},
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                last_full_reader=lambda: None,
            )

        self.assertEqual(0, code)
        polls = events.count("pending")
        flushes = events.count("flush")
        self.assertGreater(polls, 1, "这个调度下一个 cycle 应该有多次 poll，否则判据失效")
        self.assertEqual(polls, flushes)

    def test_heartbeat_watch_runs_once_per_poll_not_once_per_cycle(self) -> None:
        """心跳看护挂在哪一层，和 flush 是同一个判据，同一个坑。

        它自己内部还有一层「每小时最多查一次库」的节流。若再把调用点放到外层
        while True（一轮 = 一个完整调度周期，默认 86400s），两层节流叠起来
        就再也不会触发——而代码读起来完全正确。判据必须是频率关系
        （checks == polls），写成「checks > 0」在错误层级下照样成立。
        """
        import unittest.mock as mock

        from app.pipelines import worker_loop as module

        events: list[str] = []
        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), mock.patch.object(
            module.notify_client, "request_flush", lambda: None
        ), mock.patch.object(
            module.heartbeat_watch, "check_heartbeat", lambda: events.append("check")
        ):
            code = module.run_worker_loop(
                full_sync=lambda: events.append("full") or 0,
                consume_requests=lambda: events.append("pending") or 0,
                notifier_once=lambda: {},
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                last_full_reader=lambda: None,
            )

        self.assertEqual(0, code)
        polls = events.count("pending")
        checks = events.count("check")
        self.assertGreater(polls, 1, "这个调度下一个 cycle 应该有多次 poll，否则判据失效")
        self.assertEqual(polls, checks)

    def test_notify_flush_failure_does_not_break_the_sync_loop(self) -> None:
        """冲刷失败绝不能影响同步主循环——通知发不出去是小事，同步停了是大事。"""
        import contextlib
        import io
        import unittest.mock as mock

        from app.pipelines import worker_loop as module

        def boom() -> None:
            raise RuntimeError("synthetic-flush-failure")

        events: list[str] = []
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), mock.patch.object(
            module.notify_client, "request_flush", boom
        ), contextlib.redirect_stdout(output):
            code = module.run_worker_loop(
                full_sync=lambda: events.append("full") or 0,
                consume_requests=lambda: events.append("pending") or 0,
                notifier_once=lambda: {},
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                last_full_reader=lambda: None,
            )

        self.assertEqual(0, code)
        self.assertIn("full", events)
        self.assertIn("pending", events)
        self.assertIn("RuntimeError", output.getvalue())

    def test_notifier_failure_is_fail_open_and_logs_only_exception_type(self) -> None:
        import contextlib
        import io
        import unittest.mock as mock

        from app.pipelines.worker_loop import run_worker_loop

        events: list[str] = []

        def fail_notifier() -> dict:
            raise RuntimeError("sensitive notifier detail")

        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}):
            code = run_worker_loop(
                full_sync=lambda: events.append("full") or 0,
                consume_requests=lambda: events.append("pending") or 0,
                notifier_once=fail_notifier,
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                last_full_reader=lambda: None,
            )

        self.assertEqual(0, code)
        self.assertEqual(["full", "pending", "pending"], events)
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("sensitive notifier detail", output.getvalue())

    def test_notifier_runs_at_most_once_on_the_poll_to_full_cycle_boundary(self) -> None:
        import unittest.mock as mock
        from collections import Counter
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 12, tzinfo=timezone.utc)
        clock = {"now": started}
        events: list[tuple[int, str]] = []

        def record(event: str) -> int:
            elapsed = int((clock["now"] - started).total_seconds())
            events.append((elapsed, event))
            return 0

        def advance(seconds: float) -> None:
            clock["now"] += timedelta(seconds=seconds)
            record("sleep")

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: record("full"),
                consume_requests=lambda: record("pending"),
                notifier_once=lambda: record("notifier") or {},
                sleep=advance,
                max_cycles=2,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: self._read_and_tick(clock),
                last_full_reader=lambda: None,
            )

        notifier_times = [elapsed for elapsed, event in events if event == "notifier"]
        self.assertEqual(0, code)
        self.assertEqual([0, 30, 60, 90, 120], notifier_times)
        self.assertEqual({0: 1, 30: 1, 60: 1, 90: 1, 120: 1}, dict(Counter(notifier_times)))
        self.assertEqual([(0, "full"), (60, "full")], [event for event in events if event[1] == "full"])

    def test_disabled_terminal_poll_covers_enabled_full_at_the_same_observed_time(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 12, tzinfo=timezone.utc)
        clock = {"now": started}
        schedule_calls = {"count": 0}
        events: list[tuple[int, str]] = []

        def record(event: str) -> int:
            events.append((int((clock["now"] - started).total_seconds()), event))
            return 0

        def advance(seconds: float) -> None:
            clock["now"] += timedelta(seconds=seconds)

        def schedule_reader() -> dict:
            schedule_calls["count"] += 1
            return {
                "enabled": schedule_calls["count"] > 1,
                "interval_seconds": 60,
                "anchor_time": "",
                "pull_paused": False,
            }

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: record("full"),
                consume_requests=lambda: record("pending"),
                notifier_once=lambda: record("notifier") or {},
                sleep=advance,
                max_cycles=2,
                schedule_reader=schedule_reader,
                config_puller=lambda: "noop",
                now_fn=lambda: self._read_and_tick(clock),
                last_full_reader=lambda: None,
            )

        self.assertEqual(0, code)
        self.assertEqual([30, 60, 90, 120], [elapsed for elapsed, event in events if event == "notifier"])
        self.assertEqual([(60, "full")], [event for event in events if event[1] == "full"])

    def test_terminal_poll_at_stale_now_does_not_cover_a_future_full_preflight(self) -> None:
        import unittest.mock as mock
        from collections import Counter
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 12, tzinfo=timezone.utc)
        clock = {"now": started}
        schedule_calls = {"count": 0}
        events: list[tuple[int, str]] = []

        def record(event: str) -> int:
            events.append((int((clock["now"] - started).total_seconds()), event))
            return 0

        def schedule_reader() -> dict:
            schedule_calls["count"] += 1
            if schedule_calls["count"] == 2:
                clock["now"] = started + timedelta(seconds=60)
            return self._one_minute_schedule()

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: record("full"),
                consume_requests=lambda: record("pending"),
                notifier_once=lambda: record("notifier") or {},
                sleep=lambda _seconds: None,
                max_cycles=2,
                schedule_reader=schedule_reader,
                config_puller=lambda: "noop",
                now_fn=lambda: self._read_and_tick(clock),
                last_full_reader=lambda: started,
            )

        full_index = events.index((60, "full"))
        notifier_counts = Counter(elapsed for elapsed, event in events if event == "notifier")
        self.assertEqual(0, code)
        self.assertEqual((60, "notifier"), events[full_index - 1])
        self.assertEqual({0: 2, 60: 3}, dict(notifier_counts))

    def test_terminal_poll_with_early_sleep_does_not_cover_later_full_preflight(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 12, tzinfo=timezone.utc)
        clock = {"now": started}
        schedule_calls = {"count": 0}
        sleep_calls = {"count": 0}
        events: list[tuple[int, str]] = []

        def record(event: str) -> int:
            events.append((int((clock["now"] - started).total_seconds()), event))
            return 0

        def early_then_normal_sleep(seconds: float) -> None:
            sleep_calls["count"] += 1
            elapsed = 10 if sleep_calls["count"] <= 2 else seconds
            clock["now"] += timedelta(seconds=elapsed)

        def schedule_reader() -> dict:
            schedule_calls["count"] += 1
            if schedule_calls["count"] == 2:
                clock["now"] = started + timedelta(seconds=60, microseconds=10)
            return self._one_minute_schedule()

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: record("full"),
                consume_requests=lambda: record("pending"),
                notifier_once=lambda: record("notifier") or {},
                sleep=early_then_normal_sleep,
                max_cycles=2,
                schedule_reader=schedule_reader,
                config_puller=lambda: "noop",
                now_fn=lambda: self._read_and_tick(clock),
                last_full_reader=lambda: started,
            )

        full_index = events.index((60, "full"))
        self.assertEqual(0, code)
        self.assertEqual((60, "notifier"), events[full_index - 1])
        self.assertEqual([10, 20], [elapsed for elapsed, event in events[:4] if event == "notifier"])

    def test_nondivisible_interval_has_no_duplicate_boundary_notifier(self) -> None:
        import unittest.mock as mock
        from collections import Counter
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 12, tzinfo=timezone.utc)
        clock = {"now": started}
        notifier_times: list[int] = []

        def advance(seconds: float) -> None:
            clock["now"] += timedelta(seconds=seconds)

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: 0,
                consume_requests=lambda: 0,
                notifier_once=lambda: notifier_times.append(int((clock["now"] - started).total_seconds())) or {},
                sleep=advance,
                max_cycles=2,
                schedule_reader=lambda: {
                    "enabled": True, "interval_seconds": 65, "anchor_time": "", "pull_paused": False,
                },
                config_puller=lambda: "noop",
                now_fn=lambda: self._read_and_tick(clock),
                last_full_reader=lambda: None,
            )

        self.assertEqual(0, code)
        self.assertEqual([0, 30, 60, 65, 95, 125, 130], notifier_times)
        self.assertTrue(all(count == 1 for count in Counter(notifier_times).values()))

    def test_full_failure_does_not_stop_or_permanently_skip_notifier(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 12, tzinfo=timezone.utc)
        clock = {"now": started}
        notifier_times: list[int] = []
        full_times: list[int] = []
        events: list[tuple[int, str]] = []
        schedule_calls = {"count": 0}

        def advance(seconds: float) -> None:
            if len(full_times) != 2:
                clock["now"] += timedelta(seconds=seconds)

        def fail_full() -> int:
            elapsed = int((clock["now"] - started).total_seconds())
            full_times.append(elapsed)
            events.append((elapsed, "full"))
            if len(full_times) == 2:
                raise RuntimeError("full failed")
            return 0

        def notify() -> dict:
            elapsed = int((clock["now"] - started).total_seconds())
            notifier_times.append(elapsed)
            events.append((elapsed, "notifier"))
            return {}

        def schedule_reader() -> dict:
            schedule_calls["count"] += 1
            if schedule_calls["count"] == 3:
                clock["now"] = started + timedelta(seconds=120, microseconds=10)
            return self._one_minute_schedule()

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=fail_full,
                consume_requests=lambda: 0,
                notifier_once=notify,
                sleep=advance,
                max_cycles=3,
                schedule_reader=schedule_reader,
                config_puller=lambda: "noop",
                now_fn=lambda: self._read_and_tick(clock),
                last_full_reader=lambda: None,
            )

        third_full_index = events.index((120, "full"))
        self.assertEqual(0, code)
        self.assertEqual([0, 60, 120], full_times)
        self.assertEqual([0, 30, 60, 60, 60, 120, 150, 180], notifier_times)
        self.assertEqual((120, "notifier"), events[third_full_index - 1])

    def test_full_only_injection_without_notifier_keeps_notifier_noop(self) -> None:
        import unittest.mock as mock

        from app.pipelines import worker_loop as module

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"), \
                mock.patch.object(module, "run_pending_sync_requests", return_value=0), \
                mock.patch.object(module, "run_pending_document_locator_mirror_jobs", return_value=0), \
                mock.patch.object(module, "run_write_rnd_records", return_value=0), \
                mock.patch.object(module, "run_backfill_if_bom_synced", return_value=(None, False)), \
                mock.patch.object(module.sync_alert_notifier, "run_notifier_once") as default_notifier:
            code = module.run_worker_loop(
                full_sync=lambda: 0,
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                last_full_reader=lambda: None,
            )

        self.assertEqual(0, code)
        default_notifier.assert_not_called()

    def test_consume_only_injection_without_notifier_keeps_notifier_noop(self) -> None:
        import unittest.mock as mock

        from app.pipelines import worker_loop as module

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"), \
                mock.patch.object(module.sync_alert_notifier, "run_notifier_once") as default_notifier:
            code = module.run_worker_loop(
                consume_requests=lambda: 0,
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=lambda: {
                    "enabled": False, "interval_seconds": 60, "anchor_time": "", "pull_paused": False,
                },
                config_puller=lambda: "noop",
                last_full_reader=lambda: None,
            )

        self.assertEqual(0, code)
        default_notifier.assert_not_called()

    def test_default_pipeline_uses_production_notifier(self) -> None:
        import unittest.mock as mock

        from app.pipelines import worker_loop as module

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"), \
                mock.patch.object(module, "run_sync_wecom_full", return_value=0), \
                mock.patch.object(module, "run_backfill_images", side_effect=RuntimeError("skip")), \
                mock.patch.object(module, "run_sync_feishu_full", return_value=0), \
                mock.patch.object(module, "run_pending_document_locator_mirror_jobs", return_value=0), \
                mock.patch.object(module, "run_tplus_parent_match", return_value=0), \
                mock.patch.object(module, "run_pending_sync_requests", return_value=0), \
                mock.patch.object(module, "run_write_rnd_records", return_value=0), \
                mock.patch.object(module, "run_backfill_if_bom_synced", return_value=(None, False)), \
                mock.patch.object(module.sync_alert_notifier, "run_notifier_once", return_value={}) as default_notifier:
            code = module.run_worker_loop(
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                last_full_reader=lambda: None,
            )

        self.assertEqual(0, code)
        self.assertEqual(3, default_notifier.call_count)

    def test_loop_runs_full_then_polls_pending_requests(self) -> None:
        import os
        from app.pipelines.worker_loop import run_worker_loop

        calls = {"full": 0, "pending": 0, "slept": []}
        old_interval, old_poll = os.environ.get("DOC_SYNC_INTERVAL_SECONDS"), os.environ.get("DOC_SYNC_POLL_SECONDS")
        os.environ["DOC_SYNC_INTERVAL_SECONDS"] = "90"
        os.environ["DOC_SYNC_POLL_SECONDS"] = "30"
        try:
            code = run_worker_loop(
                full_sync=lambda: calls.__setitem__("full", calls["full"] + 1) or 0,
                consume_requests=lambda: calls.__setitem__("pending", calls["pending"] + 1) or 0,
                sleep=lambda s: calls["slept"].append(s),
                max_cycles=1,
            )
        finally:
            for key, value in (("DOC_SYNC_INTERVAL_SECONDS", old_interval), ("DOC_SYNC_POLL_SECONDS", old_poll)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(0, code)
        self.assertEqual(1, calls["full"])
        self.assertEqual(3, calls["pending"])  # 90s / 30s = 3 次轮询
        self.assertEqual([30, 30, 30], calls["slept"])

    def test_loop_survives_exceptions(self) -> None:
        import os
        from app.pipelines.worker_loop import run_worker_loop

        old_interval, old_poll = os.environ.get("DOC_SYNC_INTERVAL_SECONDS"), os.environ.get("DOC_SYNC_POLL_SECONDS")
        os.environ["DOC_SYNC_INTERVAL_SECONDS"] = "30"
        os.environ["DOC_SYNC_POLL_SECONDS"] = "30"

        def boom() -> int:
            raise RuntimeError("boom")

        try:
            code = run_worker_loop(full_sync=boom, consume_requests=boom, sleep=lambda s: None, max_cycles=2)
        finally:
            for key, value in (("DOC_SYNC_INTERVAL_SECONDS", old_interval), ("DOC_SYNC_POLL_SECONDS", old_poll)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(0, code)

    def test_loop_skips_full_sync_when_disabled_but_still_polls(self) -> None:
        from app.pipelines.worker_loop import run_worker_loop

        calls = {"full": 0, "pending": 0}
        code = run_worker_loop(
            full_sync=lambda: calls.__setitem__("full", calls["full"] + 1) or 0,
            consume_requests=lambda: calls.__setitem__("pending", calls["pending"] + 1) or 0,
            sleep=lambda s: None,
            max_cycles=2,
            schedule_reader=lambda: {"enabled": False, "interval_seconds": 90, "anchor_time": "", "pull_paused": False},
        )
        self.assertEqual(0, code)
        self.assertEqual(0, calls["full"])
        self.assertGreater(calls["pending"], 0)

    def test_loop_restart_does_not_rerun_full_sync_within_interval(self) -> None:
        from datetime import datetime, timedelta, timezone

        from app.pipelines.worker_loop import run_worker_loop

        now = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
        calls = {"full": 0}
        code = run_worker_loop(
            full_sync=lambda: calls.__setitem__("full", calls["full"] + 1) or 0,
            consume_requests=lambda: 0,
            sleep=lambda s: None,
            max_cycles=1,
            schedule_reader=lambda: {"enabled": True, "interval_seconds": 86400, "anchor_time": "", "pull_paused": False},
            now_fn=lambda: now,
            last_full_reader=lambda: now - timedelta(hours=1),  # 1 小时前刚全量过
        )
        self.assertEqual(0, code)
        self.assertEqual(0, calls["full"])

    def test_loop_waits_until_anchor_due(self) -> None:
        from datetime import datetime, timedelta, timezone

        from app.pipelines.worker_loop import run_worker_loop

        # last=UTC 07-05 18:05（北京 02:05），锚 02:00+24h → due=07-06 18:00 UTC；now=10:00 → 需等 8h。
        now = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
        slept: list[float] = []
        calls = {"full": 0}
        code = run_worker_loop(
            full_sync=lambda: calls.__setitem__("full", calls["full"] + 1) or 0,
            consume_requests=lambda: 0,
            sleep=lambda s: slept.append(s),
            max_cycles=1,
            schedule_reader=lambda: {"enabled": True, "interval_seconds": 86400, "anchor_time": "02:00", "pull_paused": False},
            now_fn=lambda: now,
            last_full_reader=lambda: datetime(2026, 7, 5, 18, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(0, code)
        self.assertEqual(0, calls["full"])
        self.assertEqual(8 * 3600, sum(slept))


    def test_default_consume_requests_carries_bom_watermark_across_polls(self) -> None:
        """_default_consume_requests 的布线此前零覆盖：5 处既有 run_worker_loop 调用全部注入了
        consume_requests，从未真正跑过默认实现里 nonlocal bom_watermark 那条线。这里不注入
        consume_requests（走默认实现），只打桩 run_backfill_if_bom_synced 验证水位真的跨 poll
        周期活在闭包里——第一次 poll 传 None，第二次传第一次的返回值。patch 的是
        app.pipelines.worker_loop 模块命名空间里的名字，因为 _default_consume_requests 内部
        都是裸名调用。"""
        import unittest.mock as mock

        from app.pipelines import worker_loop as module

        watermark_calls: list[object] = []

        def fake_backfill(last_seen):
            watermark_calls.append(last_seen)
            return (f"wm-{len(watermark_calls)}", False)

        with mock.patch.object(module, "run_backfill_if_bom_synced", side_effect=fake_backfill), \
                mock.patch.object(module, "run_pending_sync_requests", return_value=0), \
                mock.patch.object(module, "run_pending_document_locator_mirror_jobs", return_value=0), \
                mock.patch.object(module, "run_write_rnd_records", return_value=0):
            code = module.run_worker_loop(
                full_sync=lambda: 0,
                sleep=lambda s: None,
                max_cycles=2,
                schedule_reader=lambda: {
                    "enabled": False, "interval_seconds": 60, "anchor_time": "", "pull_paused": False,
                },
            )

        self.assertEqual(0, code)
        self.assertGreaterEqual(len(watermark_calls), 2)
        self.assertIsNone(watermark_calls[0])
        for i in range(1, len(watermark_calls)):
            self.assertEqual(watermark_calls[i], f"wm-{i}")


    def test_legacy_never_reads_or_writes_platform_scheduler(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        full_runs: list[datetime] = []
        platform_calls: list[str] = []

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: full_runs.append(started) or 0,
                consume_requests=lambda: 0,
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: started,
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "legacy",
                platform_schedule_reader=lambda: platform_calls.append("read") or self._one_minute_schedule(),
                platform_schedule_seeder=lambda _config: platform_calls.append("seed"),
                shadow_recorder=lambda _payload: platform_calls.append("record") or [1],
                shadow_finisher=lambda *_args: platform_calls.append("finish"),
            )

        self.assertEqual(0, code)
        self.assertEqual([started], full_runs)
        self.assertEqual([], platform_calls)

    def test_unknown_scheduler_mode_is_legacy_without_platform_access(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        full_runs: list[datetime] = []

        def platform_access_must_not_happen() -> dict:
            raise AssertionError("unknown mode must not read platform scheduling")

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: full_runs.append(started) or 0,
                consume_requests=lambda: 0,
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: started,
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "unrecognized-mode",
                platform_schedule_reader=platform_access_must_not_happen,
                platform_schedule_seeder=lambda _config: platform_access_must_not_happen(),
            )

        self.assertEqual(0, code)
        self.assertEqual([started], full_runs)

    def test_shadow_bootstraps_missing_candidate_and_records_same_preflight(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        legacy_config = self._one_minute_schedule()
        state = {"seeded": False}
        reads: list[bool] = []
        seeded: list[dict] = []
        full_runs: list[datetime] = []
        pending_calls: list[datetime] = []
        notifier_calls: list[datetime] = []
        sleeps: list[float] = []
        shadows: list[dict] = []

        def read_platform_schedule() -> dict | None:
            reads.append(state["seeded"])
            return dict(legacy_config) if state["seeded"] else None

        def seed_platform_schedule(config: dict) -> None:
            seeded.append(dict(config))
            state["seeded"] = True

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += timedelta(seconds=seconds)

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: full_runs.append(clock["now"]) or 0,
                consume_requests=lambda: pending_calls.append(clock["now"]) or 0,
                notifier_once=lambda: notifier_calls.append(clock["now"]) or {},
                sleep=advance,
                max_cycles=1,
                schedule_reader=lambda: dict(legacy_config),
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                monotonic=lambda: (clock["now"] - started).total_seconds(),
                last_full_reader=lambda: started,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=read_platform_schedule,
                platform_schedule_seeder=seed_platform_schedule,
                shadow_recorder=lambda payload: shadows.append(payload) or [81],
                shadow_finisher=lambda *_args: None,
            )

        self.assertEqual(0, code)
        self.assertEqual(
            [{"enabled": True, "interval_seconds": 60, "anchor_time": ""}],
            seeded,
        )
        self.assertEqual([False, True], reads[:2])
        self.assertEqual([], full_runs)
        self.assertEqual([30, 30], sleeps)
        self.assertEqual(2, len(pending_calls))
        self.assertEqual(2, len(notifier_calls))
        self.assertEqual(1, len(shadows))
        self.assertTrue(shadows[0]["decision_match"])
        self.assertEqual(
            (started + timedelta(seconds=60)).isoformat(),
            shadows[0]["candidate"]["due"],
        )

    def test_shadow_seed_failure_is_fail_open_to_legacy_behavior(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        events: list[str] = []

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: events.append("full") or 0,
                consume_requests=lambda: events.append("pending") or 0,
                notifier_once=lambda: events.append("notifier") or {},
                sleep=lambda _seconds: events.append("sleep"),
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: started,
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=lambda: None,
                platform_schedule_seeder=lambda _config: (_ for _ in ()).throw(RuntimeError("seed unavailable")),
                shadow_recorder=lambda _payload: events.append("record") or [1],
                shadow_finisher=lambda *_args: events.append("finish"),
            )

        self.assertEqual(0, code)
        self.assertEqual(["notifier", "full", "sleep", "pending", "notifier", "sleep", "pending", "notifier"], events)

    def test_active_bootstraps_missing_candidate_before_using_fallback(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        state = {"seeded": False}
        reads: list[bool] = []
        seeded: list[dict] = []

        def read_platform_schedule() -> dict | None:
            reads.append(state["seeded"])
            return self._one_minute_schedule() if state["seeded"] else None

        def seed_platform_schedule(config: dict) -> None:
            seeded.append(dict(config))
            state["seeded"] = True

        def advance(seconds: float) -> None:
            clock["now"] += timedelta(seconds=seconds)

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: 0,
                consume_requests=lambda: 0,
                sleep=advance,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "active",
                platform_schedule_reader=read_platform_schedule,
                platform_schedule_seeder=seed_platform_schedule,
            )

        self.assertEqual(0, code)
        self.assertEqual(
            [{"enabled": True, "interval_seconds": 60, "anchor_time": ""}],
            seeded,
        )
        self.assertEqual([False, True], reads[:2])

    def test_shadow_records_candidate_but_legacy_still_drives_run(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        full_runs: list[datetime] = []
        pending_calls: list[datetime] = []
        notifier_calls: list[datetime] = []
        shadows: list[dict] = []
        finishes: list[tuple[list[int], int, bool]] = []

        def advance(seconds: float) -> None:
            clock["now"] += timedelta(seconds=seconds)

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: full_runs.append(clock["now"]) or 0,
                consume_requests=lambda: pending_calls.append(clock["now"]) or 0,
                notifier_once=lambda: notifier_calls.append(clock["now"]) or {},
                sleep=advance,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                monotonic=lambda: (clock["now"] - started).total_seconds(),
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=lambda: {
                    "enabled": True, "interval_seconds": 120, "anchor_time": "", "pull_paused": False,
                },
                shadow_recorder=lambda payload: shadows.append(payload) or [731, 932],
                shadow_finisher=lambda run_ids, seconds, would_wake: finishes.append((run_ids, seconds, would_wake)),
            )

        self.assertEqual(0, code)
        self.assertEqual([started], full_runs)
        self.assertEqual(2, len(pending_calls))
        self.assertEqual(3, len(notifier_calls))  # full preflight + two unchanged legacy polls
        self.assertEqual(1, len(shadows))
        self.assertEqual("shadow", shadows[0]["mode"])
        self.assertFalse(shadows[0]["decision_match"])
        self.assertEqual(([731, 932], 60, False), finishes[0])

    def test_shadow_scheduler_storage_failure_is_fail_open(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        full_runs: list[datetime] = []
        record_attempts: list[dict] = []

        def fail_record(payload: dict) -> list[int]:
            record_attempts.append(payload)
            raise RuntimeError("shadow storage unavailable")

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: full_runs.append(started) or 0,
                consume_requests=lambda: 0,
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: started,
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=self._one_minute_schedule,
                shadow_recorder=fail_record,
            )

        self.assertEqual(0, code)
        self.assertEqual([started], full_runs)
        self.assertEqual(1, len(record_attempts))

    def test_shadow_finisher_failure_is_fail_open(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        full_runs: list[datetime] = []
        finish_attempts: list[tuple[list[int], int, bool]] = []

        def advance(seconds: float) -> None:
            clock["now"] += timedelta(seconds=seconds)

        def fail_finish(run_ids: list[int], seconds: int, would_wake: bool) -> None:
            finish_attempts.append((run_ids, seconds, would_wake))
            raise RuntimeError("shadow finish unavailable")

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: full_runs.append(clock["now"]) or 0,
                consume_requests=lambda: 0,
                sleep=advance,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                monotonic=lambda: (clock["now"] - started).total_seconds(),
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=self._one_minute_schedule,
                shadow_recorder=lambda _payload: [44],
                shadow_finisher=fail_finish,
            )

        self.assertEqual(0, code)
        self.assertEqual([started], full_runs)
        self.assertEqual([([44], 60, False)], finish_attempts)

    def test_shadow_records_earlier_candidate_wake_without_shortening_legacy_sleep(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        sleeps: list[float] = []
        pending_calls: list[datetime] = []
        notifier_calls: list[datetime] = []
        platform_reads = {"count": 0}
        finishes: list[tuple[list[int], int, bool]] = []

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += timedelta(seconds=seconds)

        def platform_schedule() -> dict:
            platform_reads["count"] += 1
            return {
                "enabled": True,
                "interval_seconds": 120 if platform_reads["count"] == 1 else 60,
                "anchor_time": "",
                "pull_paused": False,
            }

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: self.fail("legacy full must remain not due"),
                consume_requests=lambda: pending_calls.append(clock["now"]) or 0,
                notifier_once=lambda: notifier_calls.append(clock["now"]) or {},
                sleep=advance,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                monotonic=lambda: (clock["now"] - started).total_seconds(),
                last_full_reader=lambda: started,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=platform_schedule,
                shadow_recorder=lambda _payload: [17, 18],
                shadow_finisher=lambda run_ids, seconds, would_wake: finishes.append((run_ids, seconds, would_wake)),
            )

        self.assertEqual(0, code)
        self.assertEqual([30, 30], sleeps)
        self.assertEqual(2, len(pending_calls))
        self.assertEqual(2, len(notifier_calls))
        self.assertEqual([([17, 18], 60, True)], finishes)

    def test_active_uses_candidate_and_hot_wakes_within_one_poll(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        sleeps: list[float] = []
        full_runs: list[datetime] = []
        pending_calls: list[datetime] = []
        notifier_calls: list[datetime] = []
        platform_reads = {"count": 0}

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += timedelta(seconds=seconds)

        def platform_schedule() -> dict:
            platform_reads["count"] += 1
            return {
                "enabled": True,
                "interval_seconds": 120 if platform_reads["count"] == 1 else 60,
                "anchor_time": "",
                "pull_paused": False,
            }

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: full_runs.append(clock["now"]) or 0,
                consume_requests=lambda: pending_calls.append(clock["now"]) or 0,
                notifier_once=lambda: notifier_calls.append(clock["now"]) or {},
                sleep=advance,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: started - timedelta(seconds=60),
                scheduler_mode_reader=lambda: "active",
                platform_schedule_reader=platform_schedule,
            )

        self.assertEqual(0, code)
        self.assertEqual([], full_runs)
        self.assertEqual([30], sleeps)
        self.assertEqual(1, len(pending_calls))
        self.assertEqual(1, len(notifier_calls))

    def test_active_disabled_candidate_keeps_bounded_recheck_polls(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        sleeps: list[float] = []
        pending_calls: list[datetime] = []
        notifier_calls: list[datetime] = []

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += timedelta(seconds=seconds)

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: self.fail("disabled candidate must not run full"),
                consume_requests=lambda: pending_calls.append(clock["now"]) or 0,
                notifier_once=lambda: notifier_calls.append(clock["now"]) or {},
                sleep=advance,
                max_cycles=2,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                monotonic=lambda: (clock["now"] - started).total_seconds(),
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "active",
                platform_schedule_reader=lambda: {
                    "enabled": False, "interval_seconds": 60, "anchor_time": "", "pull_paused": False,
                },
            )

        self.assertEqual(0, code)
        self.assertEqual([30, 30, 30, 30], sleeps)
        self.assertEqual(4, len(pending_calls))
        self.assertEqual(4, len(notifier_calls))

    def test_active_hot_wake_terminal_poll_dedupes_next_preflight_notifier(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        events: list[tuple[int, str]] = []
        platform_reads = {"count": 0}

        def record(kind: str) -> int:
            events.append((int((clock["now"] - started).total_seconds()), kind))
            return 0

        def advance(seconds: float) -> None:
            clock["now"] += timedelta(seconds=seconds)

        def platform_schedule() -> dict:
            platform_reads["count"] += 1
            return {
                "enabled": True,
                "interval_seconds": 120 if platform_reads["count"] == 1 else 60,
                "anchor_time": "",
                "pull_paused": False,
            }

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: record("full"),
                consume_requests=lambda: record("pending"),
                notifier_once=lambda: record("notifier") or {},
                sleep=advance,
                max_cycles=2,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: started - timedelta(seconds=60),
                scheduler_mode_reader=lambda: "active",
                platform_schedule_reader=platform_schedule,
            )

        self.assertEqual(0, code)
        self.assertEqual([(30, "full")], [event for event in events if event[1] == "full"])
        self.assertEqual(1, sum(event == (30, "notifier") for event in events))

    def test_active_platform_read_failure_during_wait_recomputes_current_legacy_plan(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        sleeps: list[float] = []
        platform_reads = {"count": 0}

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += timedelta(seconds=seconds)

        def platform_schedule() -> dict:
            platform_reads["count"] += 1
            if platform_reads["count"] == 1:
                return {"enabled": True, "interval_seconds": 120, "anchor_time": "", "pull_paused": False}
            raise RuntimeError("platform unavailable")

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: self.fail("legacy due remains in the next outer cycle"),
                consume_requests=lambda: 0,
                sleep=advance,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                monotonic=lambda: (clock["now"] - started).total_seconds(),
                last_full_reader=lambda: started,
                scheduler_mode_reader=lambda: "active",
                platform_schedule_reader=platform_schedule,
            )

        self.assertEqual(0, code)
        self.assertEqual([30, 30], sleeps)

    def test_active_disabled_candidate_after_fallback_adopts_bounded_recheck(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        sleeps: list[float] = []
        platform_reads = {"count": 0}

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += timedelta(seconds=seconds)

        def platform_schedule() -> dict:
            platform_reads["count"] += 1
            if platform_reads["count"] == 1:
                return {"enabled": True, "interval_seconds": 120, "anchor_time": "", "pull_paused": False}
            if platform_reads["count"] == 2:
                raise RuntimeError("platform unavailable")
            return {"enabled": False, "interval_seconds": 60, "anchor_time": "", "pull_paused": False}

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: self.fail("legacy run remains not due"),
                consume_requests=lambda: 0,
                sleep=advance,
                max_cycles=2,
                schedule_reader=lambda: {
                    "enabled": True, "interval_seconds": 300, "anchor_time": "", "pull_paused": False,
                },
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: started,
                scheduler_mode_reader=lambda: "active",
                platform_schedule_reader=platform_schedule,
            )

        self.assertEqual(0, code)
        self.assertEqual([30, 30, 30, 30], sleeps)

    def test_active_post_full_platform_failure_recomputes_legacy_wait_from_new_last_full(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        sleeps: list[float] = []
        full_runs: list[datetime] = []
        platform_reads = {"count": 0}

        def advance(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += timedelta(seconds=seconds)

        def platform_schedule() -> dict:
            platform_reads["count"] += 1
            if platform_reads["count"] == 1:
                return self._one_minute_schedule()
            raise RuntimeError("platform unavailable after full")

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: full_runs.append(clock["now"]) or 0,
                consume_requests=lambda: 0,
                sleep=advance,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "active",
                platform_schedule_reader=platform_schedule,
            )

        self.assertEqual(0, code)
        self.assertEqual([started], full_runs)
        self.assertEqual([30, 30], sleeps)

    def test_shadow_due_full_records_the_new_scheduled_run_before_finishing_it(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        events: list[str] = []
        scheduled_run_ids: list[int] = []
        finished: list[tuple[list[int], int, bool]] = []

        def advance(seconds: float) -> None:
            clock["now"] += timedelta(seconds=seconds)

        def full_sync() -> int:
            events.append("full")
            scheduled_run_ids.append(901)
            return 0

        def recorder(payload: dict) -> list[int]:
            events.append("record")
            self.assertEqual([901], scheduled_run_ids)
            self.assertFalse(payload["legacy"]["run_full"])
            return list(scheduled_run_ids)

        def finisher(run_ids: list[int], seconds: int, would_wake: bool) -> None:
            events.append("finish")
            finished.append((run_ids, seconds, would_wake))

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=full_sync,
                consume_requests=lambda: 0,
                sleep=advance,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=self._one_minute_schedule,
                shadow_recorder=recorder,
                shadow_finisher=finisher,
                monotonic=lambda: (clock["now"] - started).total_seconds(),
            )

        self.assertEqual(0, code)
        self.assertEqual(["full", "record", "finish"], events)
        self.assertEqual([([901], 60, False)], finished)

    def test_shadow_due_full_resamples_decisions_at_actual_completion_time(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        payloads: list[dict] = []

        def full_sync() -> int:
            clock["now"] += timedelta(seconds=90)
            return 0

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=full_sync,
                consume_requests=lambda: 0,
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=self._one_minute_schedule,
                shadow_recorder=lambda payload: payloads.append(payload) or [901],
            )

        self.assertEqual(0, code)
        self.assertEqual(1, len(payloads))
        self.assertEqual("2026-08-13T00:01:30+00:00", payloads[0]["sampled_at"])
        self.assertEqual(
            {"due": "2026-08-13T00:01:00+00:00", "run_full": False, "wait_seconds": 60},
            payloads[0]["legacy"],
        )
        self.assertEqual(
            {"due": "2026-08-13T00:01:00+00:00", "run_full": True, "wait_seconds": 0},
            payloads[0]["candidate"],
        )
        self.assertFalse(payloads[0]["decision_match"])

    def test_shadow_failed_full_resamples_decisions_at_actual_completion_time(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        payloads: list[dict] = []

        def full_sync() -> int:
            clock["now"] += timedelta(seconds=90)
            raise RuntimeError("full elapsed failure")

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=full_sync,
                consume_requests=lambda: 0,
                sleep=lambda _seconds: None,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: None,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=self._one_minute_schedule,
                shadow_recorder=lambda payload: payloads.append(payload) or [902],
            )

        self.assertEqual(0, code)
        self.assertEqual(1, len(payloads))
        self.assertEqual("2026-08-13T00:01:30+00:00", payloads[0]["sampled_at"])
        self.assertEqual(
            {"due": "2026-08-13T00:01:00+00:00", "run_full": False, "wait_seconds": 60},
            payloads[0]["legacy"],
        )
        self.assertEqual(
            {"due": "2026-08-13T00:01:00+00:00", "run_full": True, "wait_seconds": 0},
            payloads[0]["candidate"],
        )
        self.assertFalse(payloads[0]["decision_match"])

    def test_shadow_post_full_control_flow_matches_legacy_for_elapsed_fulls(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)

        def capture(mode: str, full_elapsed_seconds: int) -> dict[str, list]:
            clock = {"now": started}
            result: dict[str, list] = {"full": [], "sleep": [], "pending": [], "notifier": [], "payload": []}

            def full_sync() -> int:
                result["full"].append(clock["now"])
                clock["now"] += timedelta(seconds=full_elapsed_seconds)
                return 0

            def sleep(seconds: float) -> None:
                result["sleep"].append(seconds)
                clock["now"] += timedelta(seconds=seconds)

            with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                    mock.patch.object(module, "_maybe_start_group_listener"):
                code = module.run_worker_loop(
                    full_sync=full_sync,
                    consume_requests=lambda: result["pending"].append(clock["now"]) or 0,
                    notifier_once=lambda: result["notifier"].append(clock["now"]) or {},
                    sleep=sleep,
                    max_cycles=2,
                    schedule_reader=self._one_minute_schedule,
                    config_puller=lambda: "noop",
                    now_fn=lambda: clock["now"],
                    last_full_reader=lambda: None,
                    scheduler_mode_reader=lambda: mode,
                    platform_schedule_reader=self._one_minute_schedule,
                    shadow_recorder=lambda payload: result["payload"].append(payload) or [991],
                )

            self.assertEqual(0, code)
            return result

        for full_elapsed_seconds in (20, 90):
            with self.subTest(full_elapsed_seconds=full_elapsed_seconds):
                legacy = capture("legacy", full_elapsed_seconds)
                shadow = capture("shadow", full_elapsed_seconds)

                self.assertEqual(legacy["sleep"], shadow["sleep"])
                self.assertEqual(len(legacy["pending"]), len(shadow["pending"]))
                self.assertEqual(len(legacy["notifier"]), len(shadow["notifier"]))
                self.assertEqual(len(legacy["full"]), len(shadow["full"]))
                # 步长不再是清一色的名义 30 秒：睡眠按 due - now() 逐步重算，
                # 全量自身耗掉的时间会从剩余等待里扣掉，所以最后一步是补齐到 due 的余数。
                # 旧实现按名义步长扣减，全量跑多久下一轮就整体后移多久（每天累积十几分钟）。
                expected = {
                    20: {"sleep": [30, 10, 30, 10], "pending": 4, "notifier": 5},
                    90: {"sleep": [30, 30], "pending": 2, "notifier": 3},
                }[full_elapsed_seconds]
                self.assertEqual(expected["sleep"], shadow["sleep"])
                self.assertEqual(expected["pending"], len(shadow["pending"]))
                self.assertEqual(expected["notifier"], len(shadow["notifier"]))
                self.assertEqual(2, len(shadow["full"]))

                first_payload = shadow["payload"][0]
                sampled_at = started + timedelta(seconds=full_elapsed_seconds)
                self.assertEqual(sampled_at.isoformat(), first_payload["sampled_at"])
                self.assertEqual(
                    {"due": "2026-08-13T00:01:00+00:00", "run_full": False, "wait_seconds": 60},
                    first_payload["legacy"],
                )
                if full_elapsed_seconds == 90:
                    self.assertTrue(first_payload["candidate"]["run_full"])
                    self.assertFalse(first_payload["decision_match"])

    def test_poll_overhead_does_not_accumulate_into_the_next_full_start(self) -> None:
        """每个 poll 干活的时间必须从剩余等待里扣掉，否则会一步步累积成起跑延迟。

        生产实测（2026-09-01）：一轮 2830 个 poll，每个多花约 0.5 秒，
        名义扣减把这些工时全漏掉，起跑时间从 anchor 的 00:30 漂到 00:54。
        这里把比例缩小成 6 个 poll × 1 秒：
        - 按 due - now() 重算：等待在 due 结束，只超出最后一个 poll 的工时 → 61 秒；
        - 按名义步长扣减：6 个 poll 各漏 1 秒 → 66 秒，且 poll 越多漏得越多。
        """
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        fulls: list[int] = []

        def poll_work() -> int:
            # 一个 poll 里 sleep 之外的活：消费手动请求 + 告警器 + 中枢 flush。
            clock["now"] += timedelta(seconds=1)
            return 0

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "10"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: fulls.append(int((clock["now"] - started).total_seconds())),
                consume_requests=poll_work,
                notifier_once=lambda: {},
                sleep=lambda seconds: clock.__setitem__("now", clock["now"] + timedelta(seconds=seconds)),
                max_cycles=2,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: started,
            )

        self.assertEqual(0, code)
        self.assertEqual([61], fulls)

    def test_shadow_advances_planned_candidate_before_later_earlier_wake(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        platform_reads = {"count": 0}
        finishes: list[tuple[list[int], int, bool]] = []

        def advance(seconds: float) -> None:
            clock["now"] += timedelta(seconds=seconds)

        def platform_schedule() -> dict:
            platform_reads["count"] += 1
            intervals = (120, 180, 140)
            return {
                "enabled": True,
                "interval_seconds": intervals[min(platform_reads["count"] - 1, len(intervals) - 1)],
                "anchor_time": "",
                "pull_paused": False,
            }

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: self.fail("legacy run remains not due"),
                consume_requests=lambda: 0,
                sleep=advance,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: started,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=platform_schedule,
                shadow_recorder=lambda _payload: [71],
                shadow_finisher=lambda run_ids, seconds, would_wake: finishes.append((run_ids, seconds, would_wake)),
                monotonic=lambda: (clock["now"] - started).total_seconds(),
            )

        self.assertEqual(0, code)
        self.assertEqual([([71], 60, True)], finishes)

    def test_shadow_observed_sleep_uses_monotonic_not_wall_clock(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timedelta, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        clock = {"now": started}
        monotonic = {"now": 0.0}
        finishes: list[tuple[list[int], int, bool]] = []

        def advance(seconds: float) -> None:
            monotonic["now"] += seconds
            clock["now"] += timedelta(hours=1)

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            code = module.run_worker_loop(
                full_sync=lambda: self.fail("legacy run remains not due"),
                consume_requests=lambda: 0,
                sleep=advance,
                max_cycles=1,
                schedule_reader=self._one_minute_schedule,
                config_puller=lambda: "noop",
                now_fn=lambda: clock["now"],
                last_full_reader=lambda: started,
                scheduler_mode_reader=lambda: "shadow",
                platform_schedule_reader=self._one_minute_schedule,
                shadow_recorder=lambda _payload: [81],
                shadow_finisher=lambda run_ids, seconds, would_wake: finishes.append((run_ids, seconds, would_wake)),
                monotonic=lambda: monotonic["now"],
            )

        self.assertEqual(0, code)
        # 墙钟每次 sleep 跳 1 小时（模拟 NTP 前跳），第一步之后就越过 due，等待结束；
        # 上报的睡眠时长必须是 monotonic 量出来的 30，不能是墙钟的 3600。
        self.assertEqual([([81], 30, False)], finishes)

    def test_shadow_sleep_error_survives_monotonic_and_finisher_failures(self) -> None:
        import unittest.mock as mock
        from datetime import datetime, timezone

        from app.pipelines import worker_loop as module

        started = datetime(2026, 8, 13, tzinfo=timezone.utc)
        now_calls = {"count": 0}
        monotonic_calls = {"count": 0}

        def unstable_now() -> datetime:
            now_calls["count"] += 1
            if now_calls["count"] > 1:
                raise RuntimeError("clock boom")
            return started

        def unstable_monotonic() -> float:
            monotonic_calls["count"] += 1
            if monotonic_calls["count"] > 1:
                raise RuntimeError("monotonic boom")
            return 0.0

        with mock.patch.dict("os.environ", {"DOC_SYNC_POLL_SECONDS": "30"}), \
                mock.patch.object(module, "_maybe_start_group_listener"):
            with self.assertRaisesRegex(RuntimeError, "sleep boom"):
                module.run_worker_loop(
                    full_sync=lambda: self.fail("legacy run remains not due"),
                    consume_requests=lambda: 0,
                    sleep=lambda _seconds: (_ for _ in ()).throw(RuntimeError("sleep boom")),
                    max_cycles=1,
                    schedule_reader=self._one_minute_schedule,
                    config_puller=lambda: "noop",
                    now_fn=unstable_now,
                    last_full_reader=lambda: started,
                    scheduler_mode_reader=lambda: "shadow",
                    platform_schedule_reader=self._one_minute_schedule,
                    shadow_recorder=lambda _payload: [91],
                    shadow_finisher=lambda *_args: (_ for _ in ()).throw(RuntimeError("finish boom")),
                    monotonic=unstable_monotonic,
                )


class SyncScheduleTests(WorkerImportTestCase):
    def test_platform_schedule_seeder_owns_store_lifecycle(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_schedule as module

        events: list[object] = []
        schedule = {"enabled": True, "interval_seconds": 86400, "anchor_time": "15:30"}

        class Store:
            def seed_platform_schedule(self, payload: dict) -> None:
                events.append(("seed", dict(payload)))

            def close(self) -> None:
                events.append("close")

        with mock.patch.object(module, "open_store", side_effect=lambda: events.append("open") or Store()):
            module.seed_platform_schedule(schedule)

        self.assertEqual(["open", ("seed", schedule), "close"], events)

    def test_platform_schedule_seeder_is_fail_open_and_closes_store(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_schedule as module

        events: list[str] = []

        class Store:
            def seed_platform_schedule(self, _payload: dict) -> None:
                events.append("seed")
                raise RuntimeError("database unavailable")

            def close(self) -> None:
                events.append("close")

        with mock.patch.object(module, "open_store", return_value=Store()):
            module.seed_platform_schedule({"enabled": True, "interval_seconds": 60, "anchor_time": ""})

        self.assertEqual(["seed", "close"], events)

    def test_parse_config_rows_accepts_typed_singleton_row(self) -> None:
        from app.pipelines.sync_schedule import parse_config_rows

        config, errors = parse_config_rows(
            [
                {
                    "配置编号": "global-default",
                    "文档同步开关": True,
                    "文档同步周期小时": "6",
                    "文档同步起点时间": "02:00",
                }
            ]
        )
        self.assertEqual({"enabled": True, "interval_seconds": 21600, "anchor_time": "02:00"}, config)
        self.assertEqual([], errors)

    def test_parse_config_rows_typed_row_rejects_bad_values(self) -> None:
        from app.pipelines.sync_schedule import parse_config_rows

        config, errors = parse_config_rows(
            [
                {
                    "配置编号": "global-default",
                    "文档同步开关": True,
                    "文档同步周期小时": "0.5",
                    "文档同步起点时间": "25:00",
                },
                {
                    "配置编号": "draft",
                    "文档同步开关": False,
                    "文档同步周期小时": "24",
                    "文档同步起点时间": "",
                },
            ]
        )
        self.assertEqual({"enabled": True}, config)
        self.assertEqual(2, len(errors))
        self.assertIn("0.5", errors[0])
        self.assertIn("25:00", errors[1])

    def test_parse_config_rows_accepts_valid_keys_and_rejects_invalid_values(self) -> None:
        from app.pipelines.sync_schedule import parse_config_rows

        config, errors = parse_config_rows(
            [
                {"配置键": "文档同步开关", "配置值": "true", "状态": "启用"},
                {"配置键": "文档同步周期小时", "配置值": "6", "状态": ""},
                {"配置键": "文档同步起点时间", "配置值": "02:00", "状态": "启用"},
                {"配置键": "未知配置", "配置值": "x", "状态": "启用"},
                {"配置键": "文档同步周期小时", "配置值": "0.5", "状态": "启用"},
            ]
        )
        self.assertEqual({"enabled": True, "interval_seconds": 21600, "anchor_time": "02:00"}, config)
        self.assertEqual(1, len(errors))
        self.assertIn("0.5", errors[0])

    def test_parse_config_rows_skips_disabled_rows_and_bad_anchor(self) -> None:
        from app.pipelines.sync_schedule import parse_config_rows

        config, errors = parse_config_rows(
            [
                {"配置键": "文档同步开关", "配置值": "false", "状态": "停用"},
                {"配置键": "文档同步起点时间", "配置值": "25:00", "状态": "启用"},
            ]
        )
        self.assertEqual({}, config)
        self.assertEqual(1, len(errors))

    def test_next_full_sync_due_without_anchor_is_last_plus_interval(self) -> None:
        from datetime import datetime, timezone

        from app.pipelines.sync_schedule import next_full_sync_due

        now = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(now, next_full_sync_due(now, None, 86400, ""))
        last = datetime(2026, 7, 6, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(
            datetime(2026, 7, 7, 1, 0, tzinfo=timezone.utc),
            next_full_sync_due(now, last, 86400, ""),
        )

    def test_next_full_sync_due_aligns_to_beijing_anchor(self) -> None:
        from datetime import datetime, timezone

        from app.pipelines.sync_schedule import next_full_sync_due

        now = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
        # 北京 02:00 = UTC 18:00（前一日）；last=UTC 07-05 18:05（北京 02:05 刚跑过）→ 下次 07-06 18:00 UTC。
        last = datetime(2026, 7, 5, 18, 5, tzinfo=timezone.utc)
        self.assertEqual(
            datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc),
            next_full_sync_due(now, last, 86400, "02:00"),
        )
        # 6h 周期锚 02:00 → 北京 02/08/14/20 相位（UTC 18/00/06/12）；last=UTC 07-06 01:00 → 下次 06:00 UTC。
        self.assertEqual(
            datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc),
            next_full_sync_due(now, datetime(2026, 7, 6, 1, 0, tzinfo=timezone.utc), 21600, "02:00"),
        )

    def test_pull_config_writes_db_when_changed_and_respects_pause(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_schedule as module

        class FakeClient:
            def list_fields(self, app_token: str, table_id: str) -> list[dict]:
                if table_id == "tbl_sync":
                    return [
                        {"field_id": "f_id", "field_title": "配置编号"},
                        {"field_id": "f_enabled", "field_title": "文档同步开关"},
                        {"field_id": "f_interval", "field_title": "文档同步周期小时"},
                        {"field_id": "f_anchor", "field_title": "文档同步起点时间"},
                    ]
                return [
                    {"field_id": "f_key", "field_title": "配置键"},
                    {"field_id": "f_val", "field_title": "配置值"},
                    {"field_id": "f_status", "field_title": "状态"},
                ]

            def get_records(self, app_token: str, table_id: str, view_id: str = "") -> dict:
                if table_id == "tbl_sync":
                    return {
                        "records": [
                            {
                                "record_id": "r_sync",
                                "fields": {
                                    "f_id": "global-default",
                                    "f_enabled": False,
                                    "f_interval": "12",
                                    "f_anchor": "03:30",
                                },
                            }
                        ],
                        "page_count": 1,
                    }
                return {
                    "records": [
                        {"record_id": "r1", "fields": {"f_key": "文档同步周期小时", "f_val": "6", "f_status": "启用"}},
                        {"record_id": "r2", "fields": {"f_key": "文档同步起点时间", "f_val": "02:00", "f_status": "启用"}},
                    ],
                    "page_count": 1,
                }

        class FakeStore:
            def __init__(self, pull_paused: bool = False) -> None:
                self.pull_paused = pull_paused
                self.saved: dict | None = None

            def list_bitable_sources(self, provider: str, env_profile: str) -> list[dict]:
                return [
                    {
                        "external_doc_id": "bascn_system_config",
                        "external_sheet_id": "tbl_sync",
                        "document_name": "系统配置",
                        "sheet_name": "同步配置",
                        "source_url": "",
                    },
                    {
                        "external_doc_id": "bascn_console",
                        "external_sheet_id": "tbl_cfg",
                        "document_name": "飞书 ChatGPT 会话管理台",
                        "sheet_name": "配置表",
                        "source_url": "",
                    },
                    {
                        "external_doc_id": "bascn_console",
                        "external_sheet_id": "tbl_other",
                        "document_name": "飞书 ChatGPT 会话管理台",
                        "sheet_name": "会话索引表",
                        "source_url": "",
                    },
                ]

            def get_sync_config(self, provider: str) -> dict:
                return {
                    "enabled": True,
                    "interval_seconds": 86400,
                    "anchor_time": "",
                    "pull_paused": self.pull_paused,
                    "updated_at": None,
                    "updated_by": "",
                }

            def upsert_sync_config(
                self, provider: str, enabled: bool, interval_seconds: int, anchor_time: str, updated_by: str
            ) -> None:
                self.saved = {
                    "provider": provider,
                    "enabled": enabled,
                    "interval_seconds": interval_seconds,
                    "anchor_time": anchor_time,
                    "updated_by": updated_by,
                }

            def close(self) -> None:
                return None

        class Cred:
            app_id = "cli_x"
            app_secret = "s"
            api_base = "https://open.feishu.cn/open-apis"

        store = FakeStore()
        with mock.patch.object(module, "open_store", return_value=store), mock.patch.object(
            module, "env_profiles", return_value=["COMPANY_A"]
        ), mock.patch.object(module, "credentials_for_profile", return_value=[Cred()]), mock.patch.object(
            module, "FeishuBitableClient", return_value=FakeClient()
        ):
            message = module.pull_config_from_bitable()
        self.assertIsNotNone(store.saved)
        self.assertEqual(43200, store.saved["interval_seconds"])
        self.assertEqual("03:30", store.saved["anchor_time"])
        self.assertEqual(False, store.saved["enabled"])
        self.assertEqual("feishu-system-config-table", store.saved["updated_by"])
        self.assertIn("applied", message)

        paused = FakeStore(pull_paused=True)
        with mock.patch.object(module, "open_store", return_value=paused), mock.patch.object(
            module, "env_profiles", return_value=["COMPANY_A"]
        ), mock.patch.object(module, "credentials_for_profile", return_value=[Cred()]), mock.patch.object(
            module, "FeishuBitableClient", return_value=FakeClient()
        ):
            message = module.pull_config_from_bitable()
        self.assertIsNone(paused.saved)
        self.assertIn("paused", message)

    def test_pull_config_falls_back_to_legacy_config_table(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_schedule as module

        class FakeClient:
            def list_fields(self, app_token: str, table_id: str) -> list[dict]:
                return [
                    {"field_id": "f_key", "field_title": "配置键"},
                    {"field_id": "f_val", "field_title": "配置值"},
                    {"field_id": "f_status", "field_title": "状态"},
                ]

            def get_records(self, app_token: str, table_id: str, view_id: str = "") -> dict:
                return {
                    "records": [
                        {"record_id": "r1", "fields": {"f_key": "文档同步周期小时", "f_val": "6", "f_status": "启用"}},
                        {"record_id": "r2", "fields": {"f_key": "文档同步起点时间", "f_val": "02:00", "f_status": "启用"}},
                    ],
                    "page_count": 1,
                }

        class FakeStore:
            saved: dict | None = None

            def list_bitable_sources(self, provider: str, env_profile: str) -> list[dict]:
                return [
                    {
                        "external_doc_id": "bascn_console",
                        "external_sheet_id": "tbl_cfg",
                        "document_name": "飞书 ChatGPT 会话管理台",
                        "sheet_name": "配置表",
                        "source_url": "",
                    }
                ]

            def get_sync_config(self, provider: str) -> dict:
                return {
                    "enabled": True,
                    "interval_seconds": 86400,
                    "anchor_time": "",
                    "pull_paused": False,
                    "updated_at": None,
                    "updated_by": "",
                }

            def upsert_sync_config(
                self, provider: str, enabled: bool, interval_seconds: int, anchor_time: str, updated_by: str
            ) -> None:
                self.saved = {
                    "provider": provider,
                    "enabled": enabled,
                    "interval_seconds": interval_seconds,
                    "anchor_time": anchor_time,
                    "updated_by": updated_by,
                }

            def close(self) -> None:
                return None

        class Cred:
            app_id = "cli_x"
            app_secret = "s"
            api_base = "https://open.feishu.cn/open-apis"

        store = FakeStore()
        with mock.patch.object(module, "open_store", return_value=store), mock.patch.object(
            module, "env_profiles", return_value=["COMPANY_A"]
        ), mock.patch.object(module, "credentials_for_profile", return_value=[Cred()]), mock.patch.object(
            module, "FeishuBitableClient", return_value=FakeClient()
        ):
            message = module.pull_config_from_bitable()

        self.assertIsNotNone(store.saved)
        self.assertEqual(21600, store.saved["interval_seconds"])
        self.assertEqual("02:00", store.saved["anchor_time"])
        self.assertEqual(True, store.saved["enabled"])
        self.assertEqual("feishu-config-table", store.saved["updated_by"])
        self.assertIn("applied", message)


class SourceNameTests(WorkerImportTestCase):
    def test_compose_source_name_keeps_document_and_sheet_names_separate(self) -> None:
        from app.storage.postgres import compose_source_name

        source_name = compose_source_name("点检表", "点检计划")

        self.assertEqual("点检表 / 点检计划", source_name)

    def test_split_source_name_recovers_legacy_document_and_sheet_names(self) -> None:
        from app.storage.postgres import split_source_name

        names = split_source_name("点检表 / 点检明细")

        self.assertEqual({"document_name": "点检表", "sheet_name": "点检明细"}, names)


class DocNameTests(WorkerImportTestCase):
    def test_get_doc_name_reads_doc_base_info(self) -> None:
        from app.providers.wecom import WeComSmartsheetClient

        class FakeClient(WeComSmartsheetClient):
            def __init__(self) -> None:
                super().__init__("corp", "secret")

            def _post(self, path: str, payload: dict) -> dict:
                assert path == "/wedoc/get_doc_base_info"
                return {"errcode": 0, "doc_base_info": {"doc_name": "产量统计", "doc_type": 10}}

        self.assertEqual("产量统计", FakeClient().get_doc_name("dc-any"))

    def test_get_doc_base_returns_name_and_modify_time(self) -> None:
        from app.providers.wecom import WeComSmartsheetClient

        class FakeClient(WeComSmartsheetClient):
            def __init__(self) -> None:
                super().__init__("corp", "secret")

            def _post(self, path: str, payload: dict) -> dict:
                return {"errcode": 0, "doc_base_info": {"doc_name": "产量统计", "modify_time": 1781234567}}

        base = FakeClient().get_doc_base("dc-any")
        self.assertEqual("产量统计", base["doc_name"])
        self.assertEqual("1781234567", base["modify_time"])

    def test_get_doc_name_returns_empty_on_api_error(self) -> None:
        from app.providers.wecom import WeComSmartsheetClient

        class FakeClient(WeComSmartsheetClient):
            def __init__(self) -> None:
                super().__init__("corp", "secret")

            def _post(self, path: str, payload: dict) -> dict:
                raise RuntimeError("/wedoc/get_doc_base_info failed: {'errcode': 301085}")

        self.assertEqual("", FakeClient().get_doc_name("dc-any"))


class EnvProfileTests(WorkerImportTestCase):
    def test_env_profiles_can_be_inferred_from_company_variables(self) -> None:
        from app.providers.wecom import env_profiles

        with patch.dict(
            "os.environ",
            {
                "WECOM_COMPANY_A_CORP_ID": "corp-a",
                "WECOM_COMPANY_A_APP_SECRET": "secret-a",
                "WEDOC_COMPANY_A_DOCID": "doc-a",
                "WECOM_COMPANY_B_CORP_ID": "corp-b",
                "WECOM_COMPANY_B_APP_SECRET": "secret-b",
            },
            clear=True,
        ):
            self.assertEqual(["COMPANY_A", "COMPANY_B"], env_profiles(""))

    def test_discover_profile_sources_ignores_placeholder_docids(self) -> None:
        from app.providers.wecom import discover_profile_sources

        with patch.dict(
            "os.environ",
            {
                "WEDOC_COMPANY_A_DOCID": "你的智能表格docid",
                "SMARTSHEET_COMPANY_A_ID": "dcFAKE_LOCAL_TEST_DOC_ID_000000000000000000000001",
            },
            clear=True,
        ):
            sources = discover_profile_sources("COMPANY_A")
            self.assertEqual(1, len(sources))
            self.assertTrue(sources[0].docid.startswith("dcFAKE_LOCAL_TEST"))

    def test_discover_profile_sources_uses_configured_smartsheet_name(self) -> None:
        from app.providers.wecom import discover_profile_sources

        with patch.dict(
            "os.environ",
            {
                "SMARTSHEET_COMPANY_B_ID": "dcFAKE_COMPANY_B_DOC_ID_000000000000000000000001",
                "SMARTSHEET_COMPANY_B_NAME": "点检表",
            },
            clear=True,
        ):
            sources = discover_profile_sources("COMPANY_B")
            self.assertEqual(1, len(sources))
            self.assertEqual("点检表", sources[0].source_name)


class FeishuProviderEnvTests(WorkerImportTestCase):
    def test_feishu_env_profiles_can_be_inferred_from_company_variables(self) -> None:
        from app.providers.feishu import env_profiles

        with patch.dict(
            "os.environ",
            {
                "FEISHU_COMPANY_A_APP_ID": "cli_a",
                "FEISHU_COMPANY_A_APP_SECRET": "secret-a",
                "FEISHU_COMPANY_B_APP_ID": "cli_b",
                "FEISHU_COMPANY_B_APP_SECRET": "secret-b",
            },
            clear=True,
        ):
            self.assertEqual(["COMPANY_A", "COMPANY_B"], env_profiles(""))

    def test_feishu_profile_discovers_bitable_source_from_env(self) -> None:
        from app.providers.feishu import credentials_for_profile, discover_profile_sources

        with patch.dict(
            "os.environ",
            {
                "FEISHU_COMPANY_A_APP_ID": "cli_a",
                "FEISHU_COMPANY_A_APP_SECRET": "secret-a",
                "FEISHU_COMPANY_A_APP_TOKEN": "bascn_test_token",
                "FEISHU_COMPANY_A_TABLE_ID": "tbl_test_table",
                "FEISHU_COMPANY_A_TABLE_NAME": "生产任务",
            },
            clear=True,
        ):
            credentials = credentials_for_profile("COMPANY_A")
            sources = discover_profile_sources("COMPANY_A")

        self.assertEqual(1, len(credentials))
        self.assertEqual("COMPANY_A", credentials[0].env_profile)
        self.assertEqual(1, len(sources))
        self.assertEqual("bascn_test_token", sources[0].app_token)
        self.assertEqual("tbl_test_table", sources[0].table_id)
        self.assertEqual("生产任务", sources[0].source_name)

    def test_feishu_session_console_bootstrap_env_is_optional_source_discovery(self) -> None:
        from app.providers.feishu import session_console_bootstrap_config

        with patch.dict(
            "os.environ",
            {
                "FEISHU_COMPANY_A_SESSION_CONSOLE_BOOTSTRAP": "true",
                "FEISHU_COMPANY_A_SESSION_CONSOLE_FOLDER_TOKEN": "fldcn_folder",
                "FEISHU_COMPANY_A_SESSION_CONSOLE_NAME": "飞书 ChatGPT 会话管理台",
            },
            clear=True,
        ):
            config = session_console_bootstrap_config("COMPANY_A")

        self.assertTrue(config.enabled)
        self.assertEqual("fldcn_folder", config.folder_token)
        self.assertEqual("飞书 ChatGPT 会话管理台", config.app_name)


class FeishuBitablePaginationTests(WorkerImportTestCase):
    def test_create_app_and_table_extracts_tokens_and_sends_schema(self) -> None:
        from app.providers.feishu import FeishuBitableClient

        class FakeClient(FeishuBitableClient):
            def __init__(self) -> None:
                super().__init__("cli_a", "secret-a")
                self._tenant_token = "tenant-token"
                self.calls: list[dict] = []

            def _request_json(self, method: str, path: str, **kwargs: object) -> dict:
                self.calls.append({"method": method, "path": path, "kwargs": kwargs})
                if path == "/bitable/v1/apps":
                    return {"code": 0, "data": {"app": {"app_token": "bascn_console", "url": "https://x/base/bascn_console"}}}
                return {"code": 0, "data": {"table": {"table_id": "tbl_sessions", "name": "会话索引表"}}}

        client = FakeClient()

        app = client.create_app("飞书 ChatGPT 会话管理台", folder_token="fldcn_folder")
        table = client.create_table(
            app.app_token,
            "会话索引表",
            fields=[{"field_name": "session_key", "type": 1}],
        )

        self.assertEqual("bascn_console", app.app_token)
        self.assertEqual("tbl_sessions", table.table_id)
        self.assertEqual("/bitable/v1/apps", client.calls[0]["path"])
        self.assertEqual({"name": "飞书 ChatGPT 会话管理台", "folder_token": "fldcn_folder"}, client.calls[0]["kwargs"]["json"])
        self.assertEqual("/bitable/v1/apps/bascn_console/tables", client.calls[1]["path"])
        self.assertEqual("会话索引表", client.calls[1]["kwargs"]["json"]["table"]["name"])
        self.assertEqual([{"field_name": "session_key", "type": 1}], client.calls[1]["kwargs"]["json"]["table"]["fields"])

    def test_get_records_merges_two_pages(self) -> None:
        from app.providers.feishu import FeishuBitableClient

        pages = [
            {"code": 0, "data": {"items": [{"record_id": "r1"}], "has_more": True, "page_token": "next"}},
            {"code": 0, "data": {"items": [{"record_id": "r2"}], "has_more": False}},
        ]

        class FakeClient(FeishuBitableClient):
            def __init__(self) -> None:
                super().__init__("cli_a", "secret-a")
                self._tenant_token = "tenant-token"
                self.calls: list[dict] = []

            def _request_json(self, method: str, path: str, **kwargs: object) -> dict:
                self.calls.append({"method": method, "path": path, "kwargs": kwargs})
                return pages.pop(0)

        client = FakeClient()

        result = client.get_records("bascn_test_token", "tbl_test_table")

        self.assertEqual(["r1", "r2"], [x["record_id"] for x in result["records"]])
        self.assertEqual(2, result["page_count"])
        self.assertEqual(2, result["fetched_count"])
        self.assertEqual("next", client.calls[1]["kwargs"]["params"]["page_token"])

    def test_get_records_errors_when_has_more_without_page_token(self) -> None:
        from app.providers.feishu import FeishuBitableClient

        class FakeClient(FeishuBitableClient):
            def __init__(self) -> None:
                super().__init__("cli_a", "secret-a")
                self._tenant_token = "tenant-token"

            def _request_json(self, method: str, path: str, **kwargs: object) -> dict:
                return {"code": 0, "data": {"items": [{"record_id": "r1"}], "has_more": True}}

        with self.assertRaisesRegex(RuntimeError, "缺少 page_token"):
            FakeClient().get_records("bascn_test_token", "tbl_test_table")

    def test_get_records_errors_when_page_token_repeats(self) -> None:
        from app.providers.feishu import FeishuBitableClient

        class FakeClient(FeishuBitableClient):
            def __init__(self) -> None:
                super().__init__("cli_a", "secret-a")
                self._tenant_token = "tenant-token"

            def _request_json(self, method: str, path: str, **kwargs: object) -> dict:
                return {"code": 0, "data": {"items": [], "has_more": True, "page_token": "repeat"}}

        with self.assertRaisesRegex(RuntimeError, "page_token 重复"):
            FakeClient().get_records("bascn_test_token", "tbl_test_table")

    def test_redact_path_hides_bitable_app_token(self) -> None:
        from app.providers.feishu import FeishuBitableClient

        path = "/bitable/v1/apps/bascn_secret_token/tables/tbl_test_table/records"

        safe_path = FeishuBitableClient._redact_path(path)

        self.assertEqual("/bitable/v1/apps/***/tables/tbl_test_table/records", safe_path)
        self.assertNotIn("bascn_secret_token", safe_path)


class FeishuBitableSyncTests(WorkerImportTestCase):
    def test_bootstrap_session_console_creates_tables_and_registers_sources(self) -> None:
        from app.pipelines.sync_feishu_full import bootstrap_session_console_sources

        class FakeClient:
            def __init__(self) -> None:
                self.created_tables: list[dict] = []

            def create_app(self, name: str, folder_token: str = "") -> object:
                self.app_name = name
                self.folder_token = folder_token

                class App:
                    app_token = "bascn_console"
                    url = "https://feishu.cn/base/bascn_console"

                return App()

            def list_tables(self, app_token: str) -> list[dict]:
                return []

            def create_table(self, app_token: str, name: str, fields: list[dict] | None = None) -> object:
                self.created_tables.append({"app_token": app_token, "name": name, "fields": fields or []})

                class Table:
                    table_id = "tbl_" + str(len(self.created_tables))

                return Table()

        class FakeStore:
            def __init__(self) -> None:
                self.sources: list[dict] = []

            def ensure_source(self, **kwargs: object) -> int:
                self.sources.append(dict(kwargs))
                return len(self.sources)

        store = FakeStore()
        client = FakeClient()

        sources = bootstrap_session_console_sources(
            store,
            client,  # type: ignore[arg-type]
            "COMPANY_A",
            app_name="飞书 ChatGPT 会话管理台",
            folder_token="fldcn_folder",
        )

        self.assertEqual("fldcn_folder", client.folder_token)
        self.assertEqual(6, len(sources))
        self.assertEqual(6, len(client.created_tables))
        self.assertEqual("会话索引表", client.created_tables[0]["name"])
        self.assertIn({"field_name": "session_key", "type": 1}, client.created_tables[0]["fields"])
        table_fields = {table["name"]: table["fields"] for table in client.created_tables}
        self.assertIn({"field_name": "默认新对话项目链接", "type": 15}, table_fields["群表"])
        self.assertIn({"field_name": "默认新对话项目名称", "type": 1}, table_fields["群表"])
        self.assertIn({"field_name": "最近名称解析时间", "type": 5}, table_fields["群表"])
        self.assertIn({"field_name": "默认新对话项目链接", "type": 15}, table_fields["用户表"])
        self.assertIn({"field_name": "默认新对话项目名称", "type": 1}, table_fields["用户表"])
        self.assertIn({"field_name": "最近名称解析时间", "type": 5}, table_fields["用户表"])
        self.assertIn(
            {
                "field_name": "对话模式默认",
                "type": 3,
                "property": {
                    "options": [
                        {"name": "极速", "color": 0},
                        {"name": "均衡", "color": 1},
                        {"name": "高级", "color": 2},
                    ]
                },
            },
            table_fields["规则配置表"],
        )
        self.assertIn({"field_name": "默认新对话项目链接", "type": 15}, table_fields["规则配置表"])
        self.assertIn({"field_name": "默认新对话项目名称", "type": 1}, table_fields["规则配置表"])
        self.assertEqual("会话索引表", store.sources[0]["sheet_name"])
        self.assertEqual("bascn_console", sources[0].app_token)

    def test_persisted_feishu_bitable_sources_are_used_without_env_table_ids(self) -> None:
        from app.pipelines.sync_feishu_full import _persisted_feishu_sources

        class FakeStore:
            def list_bitable_sources(self, provider: str, env_profile: str) -> list[dict]:
                self.provider = provider
                self.env_profile = env_profile
                return [
                    {
                        "external_doc_id": "bascn_console",
                        "external_sheet_id": "tbl_sessions",
                        "document_name": "飞书 ChatGPT 会话管理台",
                        "sheet_name": "会话索引表",
                        "source_url": "https://feishu.cn/base/bascn_console",
                    }
                ]

        sources = _persisted_feishu_sources(FakeStore(), "COMPANY_A")

        self.assertEqual(1, len(sources))
        self.assertEqual("bascn_console", sources[0].app_token)
        self.assertEqual("tbl_sessions", sources[0].table_id)
        self.assertEqual("会话索引表", sources[0].source_name)

    def test_feishu_env_and_persisted_sources_are_merged(self) -> None:
        from app.pipelines.sync_feishu_full import _merge_feishu_sources
        from app.providers.feishu import FeishuBitableSource

        sources = _merge_feishu_sources(
            [
                FeishuBitableSource(
                    env_profile="COMPANY_A",
                    app_token="bascn_env",
                    table_id="tbl_env",
                    source_name="已有表",
                )
            ],
            [
                FeishuBitableSource(
                    env_profile="COMPANY_A",
                    app_token="bascn_console",
                    table_id="tbl_sessions",
                    source_name="会话索引表",
                )
            ],
        )

        self.assertEqual(["已有表", "会话索引表"], [source.source_name for source in sources])

    def test_sync_bitable_records_upserts_managed_contact_from_session_index(self) -> None:
        from app.pipelines.sync_feishu_full import _sync_bitable_records
        from app.storage.postgres import UpsertDecision

        class FakeClient:
            def list_fields(self, app_token: str, table_id: str) -> list[dict]:
                return [
                    {"field_id": "f_session_key", "field_title": "session_key"},
                    {"field_id": "f_name", "field_title": "飞书用户名"},
                    {"field_id": "f_project", "field_title": "ChatGPT 对话链接"},
                    {"field_id": "f_project_name", "field_title": "ChatGPT 项目名"},
                    {"field_id": "f_status", "field_title": "会话状态"},
                    {"field_id": "f_current", "field_title": "是否当前会话"},
                ]

            def get_records(self, app_token: str, table_id: str, view_id: str = "") -> dict:
                return {
                    "records": [
                        {
                            "record_id": "rec_1",
                            "fields": {
                                "f_session_key": "tenant-a:user:ou_28d4",
                                "f_name": "hao",
                                "f_project": "https://chatgpt.com/g/g-p-lark/project",
                                "f_project_name": "飞书 AI 会话台",
                                "f_status": "活跃",
                                "f_current": True,
                            },
                        }
                    ],
                    "page_count": 1,
                }

        class FakeStore:
            def __init__(self) -> None:
                self.contacts: list[dict] = []

            def replace_fields(self, source_id: int, fields: list[dict]) -> dict[str, str]:
                return {str(field["field_id"]): str(field["field_title"]) for field in fields}

            def upsert_record(self, source_id: int, snapshot: object) -> UpsertDecision:
                return UpsertDecision(action="create", should_write=True)

            def upsert_managed_contact(self, contact: dict) -> None:
                self.contacts.append(dict(contact))

            def mark_source_synced(self, source_id: int) -> None:
                return None

        counts = {"sheet_count": 0, "record_count": 0, "created_count": 0, "updated_count": 0}
        store = FakeStore()

        _sync_bitable_records(
            store,
            FakeClient(),  # type: ignore[arg-type]
            1,
            "bascn_test_token",
            "tbl_test_table",
            "",
            counts,
            source_name="会话索引表",
        )

        self.assertEqual(1, len(store.contacts))
        self.assertEqual("feishu", store.contacts[0]["channel"])
        self.assertEqual("user:ou_28d4", store.contacts[0]["peer_id"])
        self.assertEqual(1, counts["managed_contact_count"])

    def test_rescan_app_tables_registers_new_tables_and_disables_missing(self) -> None:
        from app.pipelines.sync_feishu_full import _rescan_app_tables

        class FakeClient:
            def list_tables(self, app_token: str) -> list[dict]:
                return [
                    {"table_id": "tbl_sessions", "name": "会话索引表"},
                    {"table_id": "tbl_notes", "name": "使用说明"},
                ]

        class FakeStore:
            def __init__(self) -> None:
                self.sources: list[dict] = []
                self.disabled_args: tuple | None = None

            def ensure_source(self, **kwargs: object) -> int:
                self.sources.append(dict(kwargs))
                return len(self.sources)

            def disable_missing_sheets(
                self, provider: str, env_profile: str, external_doc_id: str, seen_sheet_ids: list[str]
            ) -> int:
                self.disabled_args = (provider, env_profile, external_doc_id, list(seen_sheet_ids))
                return 1

        store = FakeStore()
        pairs, disabled = _rescan_app_tables(
            store,
            FakeClient(),  # type: ignore[arg-type]
            "COMPANY_A",
            "bascn_console",
            "飞书 ChatGPT 会话管理台",
            source_url="https://feishu.cn/base/bascn_console",
            view_ids={"tbl_sessions": "view_1"},
        )

        self.assertEqual(2, len(pairs))
        self.assertEqual(1, disabled)
        source_id, source = pairs[1]
        self.assertEqual(2, source_id)
        self.assertEqual("tbl_notes", source.table_id)
        self.assertEqual("使用说明", source.sheet_name)
        self.assertEqual("view_1", pairs[0][1].view_id)
        self.assertEqual("", pairs[1][1].view_id)
        self.assertEqual("bitable_table", store.sources[0]["source_type"])
        self.assertEqual("飞书 ChatGPT 会话管理台 / 使用说明", store.sources[1]["source_name"])
        self.assertEqual(
            ("feishu", "COMPANY_A", "bascn_console", ["tbl_sessions", "tbl_notes"]),
            store.disabled_args,
        )

    def test_run_sync_feishu_full_discovers_new_tables_via_rescan(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_feishu_full as module

        class FakeClient:
            def list_tables(self, app_token: str) -> list[dict]:
                return [
                    {"table_id": "tbl_sessions", "name": "会话索引表"},
                    {"table_id": "tbl_new", "name": "cs cs cs"},
                ]

            def list_fields(self, app_token: str, table_id: str) -> list[dict]:
                return []

            def get_records(self, app_token: str, table_id: str, view_id: str = "") -> dict:
                return {"records": [], "page_count": 1}

        class FakeStore:
            def __init__(self) -> None:
                self.sources: list[dict] = []
                self.finished: dict | None = None
                self.sync_jobs = WeComManualSyncTests.FakePlatformWriter()

            def list_bitable_sources(self, provider: str, env_profile: str) -> list[dict]:
                return [
                    {
                        "external_doc_id": "bascn_console",
                        "external_sheet_id": "tbl_sessions",
                        "document_name": "飞书 ChatGPT 会话管理台",
                        "sheet_name": "会话索引表",
                        "source_url": "https://feishu.cn/base/bascn_console",
                    }
                ]

            def list_registry_doc_sources(self, provider: str, env_profile: str) -> list[dict]:
                return []

            def start_run(self, provider: str, env_profile: str, mode: str) -> int:
                return 7

            def finish_run(self, run_id: int, status: str, counts: dict, error_json: list) -> None:
                self.finished = {"status": status, "counts": dict(counts)}

            def upsert_structure_document(self, **kwargs: object) -> int:
                return 999

            def ensure_source(self, **kwargs: object) -> int:
                self.sources.append(dict(kwargs))
                return len(self.sources)

            def disable_missing_sheets(self, provider: str, env_profile: str, doc: str, seen: list) -> int:
                return 0

            def replace_fields(self, source_id: int, fields: list) -> dict:
                return {}

            def upsert_record(self, source_id: int, snapshot: object) -> object:
                raise AssertionError("no records to upsert")

            def mark_source_synced(self, source_id: int) -> None:
                return None

            def close(self) -> None:
                return None

        class Cred:
            app_id = "cli_x"
            app_secret = "s"
            api_base = "https://open.feishu.cn/open-apis"

        store = FakeStore()
        with mock.patch.object(module, "open_store", return_value=store), mock.patch.object(
            module, "env_profiles", return_value=["COMPANY_A"]
        ), mock.patch.object(module, "credentials_for_profile", return_value=[Cred()]), mock.patch.object(
            module, "FeishuBitableClient", return_value=FakeClient()
        ), mock.patch.object(module, "discover_profile_sources", return_value=[]), mock.patch.object(
            module, "reconcile_document_locators", return_value={"seen": 1}
        ) as reconcile:
            exit_code = module.run_sync_feishu_full()

        self.assertEqual(0, exit_code)
        self.assertEqual("success", store.finished["status"])
        self.assertEqual(2, store.finished["counts"]["sheet_count"])
        self.assertEqual(["feishu.doc.1", "feishu.doc.2"], [run["job_key"] for run in store.sync_jobs.started])
        self.assertEqual(["schedule", "schedule"], [run["trigger"] for run in store.sync_jobs.started])
        self.assertEqual(["success", "success"], [run["status"] for run in store.sync_jobs.finished])
        self.assertEqual(1, store.sync_jobs.reconciled)
        reconcile.assert_called_once_with(store, trigger="feishu-full")
        registered = {item["external_sheet_id"] for item in store.sources}
        self.assertIn("tbl_new", registered)


class FeishuBitableErrorTests(WorkerImportTestCase):
    def test_request_json_http_error_includes_status_without_secrets(self) -> None:
        import requests

        from app.providers.feishu import FeishuBitableClient

        class FakeResponse:
            status_code = 403
            text = "app_secret=secret-a tenant_access_token=tenant-token"

            def raise_for_status(self) -> None:
                raise requests.HTTPError("403 Client Error", response=self)

            def json(self) -> dict:
                return {}

        class FakeSession:
            trust_env = False

            def request(self, *args: object, **kwargs: object) -> FakeResponse:
                return FakeResponse()

        client = FeishuBitableClient("cli_a", "secret-a")
        client.session = FakeSession()  # type: ignore[assignment]

        with self.assertRaises(RuntimeError) as raised:
            client._request_json("GET", "/bitable/v1/apps/app/tables/table/records")

        error = str(raised.exception)
        self.assertIn("/bitable/v1/apps/***/tables/table/records", error)
        self.assertNotIn("/bitable/v1/apps/app/tables/table/records", error)
        self.assertIn("http_status=403", error)
        self.assertNotIn("secret-a", error)
        self.assertNotIn("tenant-token", error)


class WeComManualSyncTests(WorkerImportTestCase):
    class FakePlatformWriter:
        def __init__(self) -> None:
            self.started: list[dict] = []
            self.steps: list[dict] = []
            self.finished: list[dict] = []
            self.reconciled = 0

        def start_run(self, **kwargs: object) -> int:
            self.started.append(dict(kwargs))
            return 99

        def upsert_step(self, run_id: int, seq: int, name: str, status: str, **kwargs: object) -> None:
            self.steps.append({"run_id": run_id, "seq": seq, "name": name, "status": status, **kwargs})

        def finish_run(self, run_id: int, **kwargs: object) -> None:
            from app.storage.sync_job_platform import classify_error

            self.finished.append(
                {"run_id": run_id, "error_kind": classify_error(kwargs.get("error")), **kwargs}
            )

        def reconcile_document_jobs(self) -> dict[str, int]:
            self.reconciled += 1
            return {"enabled": 1, "disabled": 0}

        def successful_step_names(self) -> list[str]:
            return [step["name"] for step in self.steps if step["status"] == "success"]

    class RaisingPlatformWriter:
        def start_run(self, **kwargs: object) -> int:
            raise RuntimeError("platform unavailable")

        def upsert_step(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("platform unavailable")

        def finish_run(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("platform unavailable")

    class _Store:
        def __init__(self) -> None:
            self.sync_jobs = WeComManualSyncTests.FakePlatformWriter()
            self.finished: dict | None = None

        def get_source(self, source_id: int) -> dict | None:
            if source_id != 17:
                return None
            return {
                "id": 17,
                "provider": "wecom",
                "env_profile": "COMPANY_A",
                "source_name": "点检表 / 点检计划",
                "external_doc_id": "dc_test",
                "external_sheet_id": "sheet_test",
                "source_url": "",
                "sheet_name": "点检计划",
            }

        def start_run(self, provider: str, env_profile: str, mode: str) -> int:
            return 42

        def finish_run(self, run_id: int, status: str, counts: dict, error_json: list) -> None:
            self.finished = {"run_id": run_id, "status": status, "counts": dict(counts), "errors": list(error_json)}

        def replace_fields(self, source_id: int, fields: list) -> dict:
            return {"field_1": "名称"}

        def upsert_record(self, source_id: int, snapshot: object) -> object:
            from app.storage.postgres import UpsertDecision

            return UpsertDecision(action="create", should_write=True)

        def mark_source_synced(self, source_id: int) -> None:
            return None

    class _Client:
        def get_fields(self, docid: str, sheet_id: str) -> dict:
            return {"fields": [{"field_id": "field_1", "field_title": "名称"}]}

        def get_records(self, docid: str, sheet_id: str) -> dict:
            return {"records": [{"record_id": "rec_1", "values": {"field_1": "点检项"}}], "page_count": 1}

    @staticmethod
    def _credential() -> object:
        class Credential:
            corpid = "corp"
            secret = "secret"
            label = "test"

        return Credential()

    def _run(self, store: object, credentials: list[object] | None = None) -> tuple:
        import unittest.mock as mock

        from app.pipelines import sync_wecom_full as module

        with mock.patch.object(
            module, "credentials_for_profile", return_value=[self._credential()] if credentials is None else credentials
        ), mock.patch.object(module, "WeComSmartsheetClient", return_value=self._Client()):
            return module.sync_wecom_source(store, source_id=17, mode="manual")

    def test_wecom_manual_source_writes_running_steps_and_success(self) -> None:
        store = self._Store()

        status, legacy_run_id, _detail = self._run(store)

        self.assertEqual("success", status)
        self.assertEqual(42, legacy_run_id)
        self.assertEqual("wecom.doc.17", store.sync_jobs.started[0]["job_key"])
        self.assertEqual("manual", store.sync_jobs.started[0]["trigger"])
        self.assertEqual({"table": "sync_runs", "id": 42}, store.sync_jobs.started[0]["legacy_ref"])
        self.assertEqual(["token", "fetch_page", "normalize", "upsert"], store.sync_jobs.successful_step_names())
        self.assertEqual("success", store.sync_jobs.finished[0]["status"])

    def test_wecom_failure_finishes_platform_run_without_hiding_legacy_failure(self) -> None:
        failing_store = self._Store()

        status, legacy_run_id, _detail = self._run(failing_store, credentials=[])

        self.assertEqual("failed", status)
        self.assertEqual(42, legacy_run_id)
        self.assertEqual("failed", failing_store.sync_jobs.finished[0]["status"])
        self.assertEqual("auth", failing_store.sync_jobs.finished[0]["error_kind"])

    def test_wecom_platform_writer_failure_does_not_change_legacy_result(self) -> None:
        store = self._Store()
        store.sync_jobs = self.RaisingPlatformWriter()

        status, legacy_run_id, _detail = self._run(store)

        self.assertEqual(("success", 42), (status, legacy_run_id))
        self.assertEqual("success", store.finished["status"])


class WeComDynamicPlatformSyncTests(WorkerImportTestCase):
    class _Writer:
        def __init__(self, events: list[tuple]) -> None:
            self.events = events
            self.started: list[dict] = []
            self.steps: list[dict] = []
            self.finished: list[dict] = []

        def start_run(self, **kwargs: object) -> int:
            run_id = 100 + len(self.started)
            self.started.append({"run_id": run_id, **kwargs})
            self.events.append(("platform_start", run_id))
            return run_id

        def upsert_step(self, run_id: int, seq: int, name: str, status: str, **kwargs: object) -> None:
            step = {"run_id": run_id, "seq": seq, "name": name, "status": status, **kwargs}
            self.steps.append(step)
            self.events.append(("platform_step", run_id, seq, status))
            if status in ("success", "failed"):
                self.events.append(("platform_step_terminal", run_id, seq, status))

        def finish_run(self, run_id: int, **kwargs: object) -> None:
            from app.storage.sync_job_platform import classify_error

            self.finished.append({"run_id": run_id, "error_kind": classify_error(kwargs.get("error")), **kwargs})
            self.events.append(("platform_finish", run_id, kwargs["status"]))

    class _RaisingWriter(_Writer):
        def __init__(self, events: list[tuple], stage: str) -> None:
            super().__init__(events)
            self.stage = stage

        def start_run(self, **kwargs: object) -> int:
            if self.stage == "start":
                raise RuntimeError("platform start failed")
            return super().start_run(**kwargs)

        def upsert_step(self, run_id: int, seq: int, name: str, status: str, **kwargs: object) -> None:
            if self.stage == "step":
                raise RuntimeError("platform step failed")
            super().upsert_step(run_id, seq, name, status, **kwargs)

        def finish_run(self, run_id: int, **kwargs: object) -> None:
            if self.stage == "finish":
                raise RuntimeError("platform finish failed")
            super().finish_run(run_id, **kwargs)

    class _Store:
        def __init__(self, *, failing_sheet: str = "", writer_stage: str = "") -> None:
            self.events: list[tuple] = []
            self.sync_jobs = (
                WeComDynamicPlatformSyncTests._RaisingWriter(self.events, writer_stage)
                if writer_stage
                else WeComDynamicPlatformSyncTests._Writer(self.events)
            )
            self.failing_sheet = failing_sheet
            self.finished: dict | None = None
            self.sources: list[dict] = []

        def get_source(self, source_id: int) -> dict | None:
            return {
                "id": 70,
                "provider": "wecom",
                "env_profile": "COMPANY_A",
                "source_name": "生产点检表",
                "external_doc_id": "dc_parent",
                "external_sheet_id": "",
                "source_url": "",
                "sheet_name": "",
            } if source_id == 70 else None

        def start_run(self, provider: str, env_profile: str, mode: str) -> int:
            return 42

        def finish_run(self, run_id: int, status: str, counts: dict, error_json: list) -> None:
            self.finished = {"run_id": run_id, "status": status, "counts": dict(counts), "errors": list(error_json)}
            self.events.append(("legacy_finish", status))

        def ensure_source(self, **kwargs: object) -> int:
            self.sources.append(dict(kwargs))
            return 101 if kwargs["external_sheet_id"] == "sheet_a" else 102

        def get_doc_modified(self, provider: str, env_profile: str, docid: str) -> str:
            return ""

        def replace_fields(self, source_id: int, fields: list) -> dict:
            return {"field_1": "名称"}

        def upsert_record(self, source_id: int, snapshot: object) -> object:
            from app.storage.postgres import UpsertDecision

            return UpsertDecision(action="create", should_write=True)

        def mark_source_synced(self, source_id: int) -> None:
            return None

        def disable_missing_sheets(self, provider: str, env_profile: str, docid: str, sheet_ids: list) -> int:
            return 0

        def upsert_doc_source(self, **kwargs: object) -> None:
            return None

        def list_registry_doc_sources(self, provider: str, env_profile: str) -> list[dict]:
            return []

        def close(self) -> None:
            return None

    class _Client:
        def __init__(self, failing_sheet: str = "") -> None:
            self.failing_sheet = failing_sheet

        def get_doc_base(self, docid: str) -> dict:
            return {"doc_name": "生产点检表", "modify_time": "1"}

        def get_sheets(self, docid: str) -> list[dict]:
            return [{"sheet_id": "sheet_a", "title": "A表"}, {"sheet_id": "sheet_b", "title": "B表"}]

        def get_fields(self, docid: str, sheet_id: str) -> dict:
            return {"fields": [{"field_id": "field_1", "field_title": "名称"}]}

        def get_records(self, docid: str, sheet_id: str) -> dict:
            if sheet_id == self.failing_sheet:
                raise RuntimeError("access_token=secret-value docid=dc_sensitive")
            if self.failing_sheet == "normalize" and sheet_id == "sheet_b":
                return {"records": [{"record_id": "rec_bad", "values": {"field_1": {"bad": {1}}}}], "page_count": 1}
            return {"records": [{"record_id": f"rec_{sheet_id}", "values": {"field_1": sheet_id}}], "page_count": 1}

    @staticmethod
    def _credential() -> object:
        class Credential:
            corpid = "corp"
            secret = "secret"
            label = "test"

        return Credential()

    def _manual_doc(self, store: object) -> tuple:
        import unittest.mock as mock

        from app.pipelines import sync_wecom_full as module

        with mock.patch.object(module, "credentials_for_profile", return_value=[self._credential()]), mock.patch.object(
            module, "WeComSmartsheetClient", return_value=self._Client(store.failing_sheet)
        ):
            return module.sync_wecom_source(store, source_id=70, mode="manual")

    def test_doc_manual_records_two_dynamic_jobs_before_finishing_them_after_legacy(self) -> None:
        store = self._Store()

        status, run_id, _detail = self._manual_doc(store)

        self.assertEqual(("success", 42), (status, run_id))
        self.assertEqual(["wecom.doc.101", "wecom.doc.102"], [item["job_key"] for item in store.sync_jobs.started])
        self.assertEqual([101, 102], [item["source_id"] for item in store.sync_jobs.started])
        self.assertEqual(["manual", "manual"], [item["trigger"] for item in store.sync_jobs.started])
        self.assertEqual([{"table": "sync_runs", "id": 42}] * 2, [item["legacy_ref"] for item in store.sync_jobs.started])
        for platform_run_id, sheet_name in ((100, "A表"), (101, "B表")):
            steps = [step for step in store.sync_jobs.steps if step["run_id"] == platform_run_id and step["status"] == "success"]
            self.assertEqual([(1, "token", 1, ""), (2, "list_sheets", 2, ""), (3, "fetch_page", 1, sheet_name), (4, "normalize", 1, sheet_name), (5, "upsert", 1, sheet_name)], [(step["seq"], step["name"], step["items"], step["message"]) for step in steps])
        self.assertEqual([(1, 1), (1, 1)], [(item["row_count"], item["changed_count"]) for item in store.sync_jobs.finished])
        normalize_success = next(index for index, event in enumerate(store.events) if event == ("platform_step", 100, 4, "success"))
        upsert_running = next(index for index, event in enumerate(store.events) if event == ("platform_step", 100, 5, "running"))
        self.assertLess(normalize_success, upsert_running)
        legacy_index = next(index for index, event in enumerate(store.events) if event[0] == "legacy_finish")
        self.assertTrue(all(index > legacy_index for index, event in enumerate(store.events) if event[0] == "platform_finish"))

    def test_full_schedule_partial_defers_failed_dynamic_terminal_and_redacts_error(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_wecom_full as module
        from app.providers.wecom import WeComDocSource

        store = self._Store(failing_sheet="sheet_b")
        with mock.patch.object(module, "open_store", return_value=store), mock.patch.object(
            module, "env_profiles", return_value=["COMPANY_A"]
        ), mock.patch.object(module, "credentials_for_profile", return_value=[self._credential()]), mock.patch.object(
            module, "discover_profile_sources", return_value=[WeComDocSource("COMPANY_A", "dc_parent", "生产点检表", "")]
        ), mock.patch.object(module, "WeComSmartsheetClient", return_value=self._Client("sheet_b")), mock.patch.object(
            module, "reconcile_document_locators", return_value={"seen": 1}
        ) as reconcile, mock.patch.object(module, "record_locator_failure", return_value=True) as record_failure:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(1, module.run_sync_wecom_full())

        self.assertEqual("partial_failed", store.finished["status"])
        reconcile.assert_not_called()
        record_failure.assert_called_once_with(store, source_id=None, error=unittest.mock.ANY)
        self.assertEqual(["schedule", "schedule"], [item["trigger"] for item in store.sync_jobs.started])
        self.assertEqual(["success", "partial"], [item["status"] for item in store.sync_jobs.finished])
        failed_step = next(step for step in store.sync_jobs.steps if step["run_id"] == 101 and step["status"] == "failed")
        self.assertEqual((3, "fetch_page", 0, "B表"), (failed_step["seq"], failed_step["name"], failed_step["items"], failed_step["message"]))
        partial = store.sync_jobs.finished[1]
        self.assertEqual("auth", partial["error_kind"])
        self.assertNotIn("secret-value", str(partial["error"]))
        self.assertNotIn("dc_sensitive", str(partial["error"]))
        persisted_and_logged = str(store.finished) + output.getvalue()
        self.assertNotIn("secret-value", persisted_and_logged)
        self.assertNotIn("dc_sensitive", persisted_and_logged)
        self.assertNotIn("dc_parent", persisted_and_logged)
        legacy_index = next(index for index, event in enumerate(store.events) if event[0] == "legacy_finish")
        failed_step_index = next(index for index, event in enumerate(store.events) if event == ("platform_step_terminal", 101, 3, "failed"))
        self.assertGreater(failed_step_index, legacy_index)
        terminal_indexes = [index for index, event in enumerate(store.events) if event[0] in {"platform_step_terminal", "platform_finish"}]
        self.assertTrue(all(index > legacy_index for index in terminal_indexes if store.events[index][0] == "platform_finish"))

    def test_unchanged_live_read_clears_stale_locator_permission_failure(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_wecom_full as module

        store = self._Store()
        store.get_doc_modified = lambda *_args: "1"
        counts = {"skipped_doc_count": 0}
        with mock.patch.object(module, "record_locator_read_success", return_value=True) as restored:
            module._sync_doc(
                store,
                self._Client(),
                profile="COMPANY_A",
                docid="dc_parent",
                fallback_name="生产点检表",
                source_url="",
                counts=counts,
                errors=[],
                skip_unchanged=True,
            )

        restored.assert_called_once_with(store, env_profile="COMPANY_A", api_doc_id="dc_parent")
        self.assertEqual(1, counts["skipped_doc_count"])

    def test_dynamic_writer_failures_are_fail_open_for_each_stage(self) -> None:
        for stage in ("start", "step", "finish"):
            with self.subTest(stage=stage):
                store = self._Store(writer_stage=stage)

                status, run_id, _detail = self._manual_doc(store)

                self.assertEqual(("success", 42), (status, run_id))
                self.assertEqual("success", store.finished["status"])

    def test_normalize_failure_keeps_its_sheet_message_and_never_starts_upsert(self) -> None:
        store = self._Store(failing_sheet="normalize")

        status, run_id, _detail = self._manual_doc(store)

        self.assertEqual(("partial_failed", 42), (status, run_id))
        failed_step = next(step for step in store.sync_jobs.steps if step["run_id"] == 101 and step["status"] == "failed")
        self.assertEqual((4, "normalize", 0, "B表"), (failed_step["seq"], failed_step["name"], failed_step["items"], failed_step["message"]))
        self.assertFalse(any(step["run_id"] == 101 and step["seq"] == 5 for step in store.sync_jobs.steps))


class FeishuManualSyncTests(WorkerImportTestCase):
    class _PlatformWriter:
        def __init__(self, events: list[tuple]) -> None:
            self.events = events
            self.started: list[dict] = []
            self.steps: list[dict] = []
            self.finished: list[dict] = []

        def start_run(self, **kwargs: object) -> int:
            run_id = 100 + len(self.started)
            self.started.append({"run_id": run_id, **kwargs})
            self.events.append(("platform_start", run_id))
            return run_id

        def upsert_step(self, run_id: int, seq: int, name: str, status: str, **kwargs: object) -> None:
            step = {"run_id": run_id, "seq": seq, "name": name, "status": status, **kwargs}
            self.steps.append(step)
            self.events.append(("platform_step", run_id, seq, status))
            if status in ("success", "failed"):
                self.events.append(("platform_step_terminal", run_id, seq, status))

        def finish_run(self, run_id: int, **kwargs: object) -> None:
            from app.storage.sync_job_platform import classify_error

            self.finished.append({"run_id": run_id, "error_kind": classify_error(kwargs.get("error")), **kwargs})
            self.events.append(("platform_finish", run_id, kwargs["status"]))

        def successful_step_names(self) -> list[str]:
            return [step["name"] for step in self.steps if step["status"] == "success"]

    class _RaisingPlatformWriter(_PlatformWriter):
        def __init__(self, events: list[tuple], stage: str) -> None:
            super().__init__(events)
            self.stage = stage

        def start_run(self, **kwargs: object) -> int:
            if self.stage == "start":
                raise RuntimeError("platform start failed")
            return super().start_run(**kwargs)

        def upsert_step(self, run_id: int, seq: int, name: str, status: str, **kwargs: object) -> None:
            if self.stage == "step":
                raise RuntimeError("platform step failed")
            super().upsert_step(run_id, seq, name, status, **kwargs)

        def finish_run(self, run_id: int, **kwargs: object) -> None:
            if self.stage == "finish":
                raise RuntimeError("platform finish failed")
            super().finish_run(run_id, **kwargs)

    class _Store:
        """sync_feishu_source 所需的最小 FakeStore。"""

        def __init__(self, source: dict, writer_stage: str = "") -> None:
            self.source = source
            self.runs: list[dict] = []
            self.finished: dict | None = None
            self.sources: list[dict] = []
            self.synced_source_ids: list[int] = []
            self.events: list[tuple] = []
            self.sync_jobs = (
                FeishuManualSyncTests._RaisingPlatformWriter(self.events, writer_stage)
                if writer_stage
                else FeishuManualSyncTests._PlatformWriter(self.events)
            )

        def get_source(self, source_id: int) -> dict | None:
            return dict(self.source) if source_id == self.source["id"] else None

        def start_run(self, provider: str, env_profile: str, mode: str) -> int:
            self.runs.append({"provider": provider, "env_profile": env_profile, "mode": mode})
            return 42

        def finish_run(self, run_id: int, status: str, counts: dict, error_json: list) -> None:
            self.finished = {"run_id": run_id, "status": status, "counts": dict(counts), "errors": list(error_json)}
            self.events.append(("legacy_finish", status))

        def ensure_source(self, **kwargs: object) -> int:
            self.sources.append(dict(kwargs))
            return len(self.sources)

        def disable_missing_sheets(self, provider: str, env_profile: str, external_doc_id: str, seen: list) -> int:
            return 0

        def replace_fields(self, source_id: int, fields: list) -> dict:
            return {}

        def upsert_record(self, source_id: int, snapshot: object) -> object:
            from app.storage.postgres import UpsertDecision

            return UpsertDecision(action="create", should_write=True)

        def mark_source_synced(self, source_id: int) -> None:
            self.synced_source_ids.append(source_id)
            return None

    class _Client:
        def list_tables(self, app_token: str) -> list[dict]:
            return [{"table_id": "tbl_a", "name": "会话索引表"}, {"table_id": "tbl_b", "name": "使用说明"}]

        def list_fields(self, app_token: str, table_id: str) -> list[dict]:
            return []

        def get_records(self, app_token: str, table_id: str, view_id: str = "") -> dict:
            return {"records": [{"record_id": f"rec_{table_id}", "fields": {}}], "page_count": 1}

    def _run(self, source: dict, client: object | None = None, writer_stage: str = "") -> tuple:
        import unittest.mock as mock

        from app.pipelines import sync_feishu_full as module

        # credentials 只取 [0] 的 app_id/app_secret/api_base，用简单对象即可
        class Cred:
            app_id = "cli_x"
            app_secret = "s"
            api_base = "https://open.feishu.cn/open-apis"

        store = self._Store(source, writer_stage=writer_stage)
        with mock.patch.object(module, "credentials_for_profile", return_value=[Cred()]), mock.patch.object(
            module, "FeishuBitableClient", return_value=client or self._Client()
        ):
            result = module.sync_feishu_source(store, source_id=source["id"], mode="manual")
        return store, result

    @staticmethod
    def _table_source() -> dict:
        return {
            "id": 9,
            "provider": "feishu",
            "env_profile": "COMPANY_A",
            "source_name": "飞书 ChatGPT 会话管理台 / 消息日志表",
            "source_type": "bitable_table",
            "external_doc_id": "bascn_console",
            "external_sheet_id": "tbl_messages",
            "source_url": "",
            "status": "active",
            "sheet_name": "消息日志表",
        }

    def _run_doc_with_one_failed_table(self) -> tuple:
        class FailingClient(self._Client):
            def get_records(self, app_token: str, table_id: str, view_id: str = "") -> dict:
                if table_id == "tbl_a":
                    raise RuntimeError(
                        "HTTP 429 too many requests app_token=bascn_fake "
                        "Authorization: Bearer token-value access_token=token-x raw-failure-marker"
                    )
                return super().get_records(app_token, table_id, view_id)

        return self._run(
            {
                "id": 1619,
                "provider": "feishu",
                "env_profile": "COMPANY_A",
                "source_name": "飞书 ChatGPT 会话管理台",
                "source_type": "smartsheet_doc",
                "external_doc_id": "bascn_console",
                "external_sheet_id": "",
                "source_url": "",
                "status": "active",
                "sheet_name": "",
            },
            client=FailingClient(),
        )

    def test_doc_level_request_rescans_and_syncs_all_tables(self) -> None:
        store, (status, run_id, detail) = self._run(
            {
                "id": 1619,
                "provider": "feishu",
                "env_profile": "COMPANY_A",
                "source_name": "飞书 ChatGPT 会话管理台",
                "source_type": "smartsheet_doc",
                "external_doc_id": "bascn_console",
                "external_sheet_id": "",
                "source_url": "",
                "status": "active",
                "sheet_name": "",
            }
        )
        self.assertEqual("success", status)
        self.assertEqual(42, run_id)
        self.assertEqual(2, len(store.sources))
        self.assertEqual("manual", store.runs[0]["mode"])
        self.assertEqual(2, store.finished["counts"]["sheet_count"])
        self.assertEqual(2, store.finished["counts"]["record_count"])

    def test_table_level_request_syncs_single_table(self) -> None:
        store, (status, run_id, detail) = self._run(
            {
                "id": 1218,
                "provider": "feishu",
                "env_profile": "COMPANY_A",
                "source_name": "飞书 ChatGPT 会话管理台 / 消息日志表",
                "source_type": "bitable_table",
                "external_doc_id": "bascn_console",
                "external_sheet_id": "tbl_messages",
                "source_url": "",
                "status": "active",
                "sheet_name": "消息日志表",
            }
        )
        self.assertEqual("success", status)
        self.assertEqual(0, len(store.sources))  # 单表请求不重扫
        self.assertEqual(1, store.finished["counts"]["sheet_count"])

    def test_feishu_table_request_dual_writes_with_legacy_ref(self) -> None:
        store, (status, run_id, _detail) = self._run(self._table_source())

        self.assertEqual(("success", 42), (status, run_id))
        started = store.sync_jobs.started[0]
        self.assertEqual("feishu.doc.9", started["job_key"])
        self.assertEqual(9, started["source_id"])
        self.assertEqual("manual", started["trigger"])
        self.assertEqual({"table": "sync_runs", "id": 42}, started["legacy_ref"])
        self.assertEqual(["token", "fetch_page", "normalize", "upsert"], store.sync_jobs.successful_step_names())
        fetch_page = next(step for step in store.sync_jobs.steps if step["name"] == "fetch_page" and step["status"] == "success")
        self.assertEqual((1, "1"), (fetch_page["items"], fetch_page["message"]))
        finished = store.sync_jobs.finished[0]
        self.assertEqual(("success", 1, 1), (finished["status"], finished["row_count"], finished["changed_count"]))

    def test_feishu_partial_maps_to_platform_partial(self) -> None:
        store, (status, _run_id, _detail) = self._run_doc_with_one_failed_table()

        self.assertEqual("partial_failed", status)
        self.assertEqual([2], store.synced_source_ids)
        started_by_source = {item["source_id"]: item["run_id"] for item in store.sync_jobs.started}
        finished_by_run = {item["run_id"]: item for item in store.sync_jobs.finished}
        failed = finished_by_run[started_by_source[1]]
        succeeded = finished_by_run[started_by_source[2]]
        self.assertEqual("partial", failed["status"])
        self.assertEqual("success", succeeded["status"])
        self.assertNotIn("bascn_fake", str(failed["error"]))
        self.assertNotIn("token-value", str(failed["error"]))
        self.assertNotIn("token-x", str(failed["error"]))
        self.assertNotIn("raw-failure-marker", str(failed["error"]))
        self.assertNotIn("bascn_fake", str(failed["detail_json"]))
        self.assertNotIn("raw-failure-marker", str(failed["detail_json"]))
        legacy_index = next(index for index, event in enumerate(store.events) if event[0] == "legacy_finish")
        failed_step_index = next(
            index
            for index, event in enumerate(store.events)
            if event == ("platform_step_terminal", started_by_source[1], 3, "failed")
        )
        failed_run_index = next(
            index
            for index, event in enumerate(store.events)
            if event == ("platform_finish", started_by_source[1], "partial")
        )
        self.assertLess(legacy_index, failed_step_index)
        self.assertLess(legacy_index, failed_run_index)

    def test_feishu_platform_writer_failures_do_not_change_legacy_success(self) -> None:
        for stage in ("start", "step", "finish"):
            with self.subTest(stage=stage):
                store, (status, run_id, _detail) = self._run(self._table_source(), writer_stage=stage)

                self.assertEqual(("success", 42), (status, run_id))
                self.assertEqual("success", store.finished["status"])

    def test_non_feishu_source_fails_without_run(self) -> None:
        from app.pipelines.sync_feishu_full import sync_feishu_source

        store = self._Store(
            {
                "id": 9,
                "provider": "wecom",
                "env_profile": "COMPANY_A",
                "source_name": "x",
                "source_type": "smartsheet_doc",
                "external_doc_id": "dc",
                "external_sheet_id": "",
                "source_url": "",
                "status": "active",
                "sheet_name": "",
            }
        )
        status, run_id, detail = sync_feishu_source(store, source_id=9)
        self.assertEqual("failed", status)
        self.assertIsNone(run_id)
        self.assertEqual([], store.runs)


class SyncRequestDispatchTests(WorkerImportTestCase):
    def test_pending_requests_dispatch_by_provider(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_wecom_full as module

        class FakeStore:
            def __init__(self) -> None:
                self.finished: list[tuple] = []
                self.sync_jobs = WeComManualSyncTests.FakePlatformWriter()

            def pending_sync_requests(self, limit: int) -> list[dict]:
                return [
                    {"id": 1, "source_id": 1619, "provider": "feishu", "env_profile": "COMPANY_A", "mode": "manual"},
                    {"id": 2, "source_id": 100, "provider": "wecom", "env_profile": "COMPANY_B", "mode": "manual"},
                ]

            def mark_sync_request_running(self, request_id: int) -> None:
                return None

            def finish_sync_request(self, request_id: int, status: str, run_id: object, detail: dict) -> None:
                self.finished.append((request_id, status))

            def close(self) -> None:
                return None

        store = FakeStore()
        calls: list[tuple[str, int]] = []

        def fake_feishu(s: object, source_id: int, mode: str = "manual") -> tuple:
            calls.append(("feishu", source_id))
            return "success", 42, {}

        def fake_wecom(s: object, source_id: int, mode: str = "manual") -> tuple:
            calls.append(("wecom", source_id))
            return "success", 43, {}

        with mock.patch.object(module, "open_store", return_value=store), mock.patch.object(
            module, "sync_feishu_source", side_effect=fake_feishu
        ), mock.patch.object(module, "sync_wecom_source", side_effect=fake_wecom), mock.patch.object(
            module, "record_locator_after_request", return_value=True
        ) as record_locator:
            exit_code = module.run_pending_sync_requests(limit=10)

        self.assertEqual(0, exit_code)
        self.assertEqual([("feishu", 1619), ("wecom", 100)], calls)
        self.assertEqual([(1, "success"), (2, "success")], store.finished)
        self.assertEqual(
            [mock.call(store, request, "success") for request in store.pending_sync_requests(10)],
            record_locator.call_args_list,
        )
        self.assertEqual(1, store.sync_jobs.reconciled)

    def test_failed_request_does_not_mark_locator_verified(self) -> None:
        import unittest.mock as mock

        from app.pipelines import sync_wecom_full as module

        class FakeStore:
            sync_jobs = WeComManualSyncTests.FakePlatformWriter()

            def pending_sync_requests(self, limit: int) -> list[dict]:
                del limit
                return [{"id": 3, "source_id": 100, "provider": "wecom", "mode": "manual"}]

            def mark_sync_request_running(self, request_id: int) -> None:
                del request_id

            def finish_sync_request(self, *args: object) -> None:
                del args

            def close(self) -> None:
                return None

        store = FakeStore()
        with mock.patch.object(module, "open_store", return_value=store), mock.patch.object(
            module, "sync_wecom_source", return_value=("failed", 44, {"error": "synthetic"})
        ), mock.patch.object(module, "record_locator_after_request", return_value=False) as record_locator:
            self.assertEqual(1, module.run_pending_sync_requests(limit=10))

        record_locator.assert_called_once_with(
            store,
            {"id": 3, "source_id": 100, "provider": "wecom", "mode": "manual"},
            "failed",
        )


class _HeartbeatCursor:
    def __init__(self, latest: object) -> None:
        self.latest = latest
        self.executed: list[tuple] = []

    def __enter__(self) -> "_HeartbeatCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple:
        return (self.latest,)


class _HeartbeatConn:
    def __init__(self, latest: object) -> None:
        self.latest = latest
        self.cursors: list[_HeartbeatCursor] = []
        self.closed = False

    def cursor(self) -> _HeartbeatCursor:
        cur = _HeartbeatCursor(self.latest)
        self.cursors.append(cur)
        return cur

    def close(self) -> None:
        self.closed = True


class AliecsHeartbeatWatchTests(WorkerImportTestCase):
    """aliecs 流量心跳的反向看护。

    它守的是「设备该发的日报没来」，而不是流量数字本身——因为「出网被限速」时
    设备侧的告警自己也发不出去，只有接收端看得见。
    """

    WATCHED = "aliecs-traffic"

    def _enabled_env(self, **extra: str) -> dict:
        env = {
            "ALIECS_TRAFFIC_HEARTBEAT_MAX_AGE_HOURS": "30",
            "ALIECS_TRAFFIC_HEARTBEAT_SOURCE": self.WATCHED,
        }
        env.update(extra)
        return env

    def test_disabled_by_default_never_touches_the_database(self) -> None:
        """默认关闭：采集器还没装时打开，只会天天报「还没上线」这种假告警。"""
        from unittest import mock

        from app.pipelines import heartbeat_watch as module

        module.reset_throttle()
        # 空值走 _env_float 的默认分支 → 阈值 0 → 关闭，与「env 根本没设」等价。
        with mock.patch.dict(
            "os.environ", {"ALIECS_TRAFFIC_HEARTBEAT_MAX_AGE_HOURS": ""}
        ), mock.patch.object(module.notify_client, "connect") as connect:
            self.assertFalse(module.check_heartbeat())
        connect.assert_not_called()

    def test_throttle_limits_database_hits_to_one_per_interval(self) -> None:
        """每轮 poll 都调它，但查库按小时节流——否则 30 秒一次白打数据库。"""
        from datetime import datetime, timedelta, timezone
        from unittest import mock

        from app.pipelines import heartbeat_watch as module

        module.reset_throttle()
        fresh = datetime.now(timezone.utc) - timedelta(hours=1)
        conn = _HeartbeatConn(fresh)
        with mock.patch.dict("os.environ", self._enabled_env()), mock.patch.object(
            module.notify_client, "connect", return_value=conn
        ) as connect:
            hits = [module.check_heartbeat() for _ in range(5)]
        self.assertEqual([True, False, False, False, False], hits)
        self.assertEqual(1, connect.call_count)

    def test_fresh_heartbeat_does_not_alert(self) -> None:
        from datetime import datetime, timedelta, timezone
        from unittest import mock

        from app.pipelines import heartbeat_watch as module

        module.reset_throttle()
        conn = _HeartbeatConn(datetime.now(timezone.utc) - timedelta(hours=2))
        with mock.patch.dict("os.environ", self._enabled_env()), mock.patch.object(
            module.notify_client, "connect", return_value=conn
        ), mock.patch.object(module.notify_client, "enqueue") as enqueue:
            module.check_heartbeat(force=True)
        enqueue.assert_not_called()
        self.assertTrue(conn.closed)

    def test_stale_heartbeat_alerts_with_a_per_day_dedup_key(self) -> None:
        """断了多久都只在每天第一次检查时留一条，不刷屏。"""
        from datetime import datetime, timedelta, timezone
        from unittest import mock

        from app.pipelines import heartbeat_watch as module

        now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
        conn = _HeartbeatConn(now - timedelta(hours=40))
        keys = []
        with mock.patch.dict("os.environ", self._enabled_env()), mock.patch.object(
            module.notify_client, "connect", return_value=conn
        ), mock.patch.object(module.notify_client, "enqueue") as enqueue:
            module.check_heartbeat(now=now, force=True)
            module.check_heartbeat(now=now + timedelta(hours=6), force=True)
            keys = [call.kwargs["dedup_key"] for call in enqueue.call_args_list]

        self.assertEqual(2, len(keys))
        self.assertEqual(keys[0], keys[1])
        self.assertEqual("aliecs-traffic-heartbeat-missing:2026-09-03", keys[0])

    def test_alert_is_never_written_under_the_watched_source(self) -> None:
        """反证核心：告警若用被监视的 source_key 写回 outbox，它自己就把心跳「续上」了。

        下一轮检查会看到「最近有行」于是不再告警——自己消掉自己的触发条件，
        而 outbox 有行、deliveries 有行、看护也在跑，三处观测面全部正常。
        同族见 AGENTS.md〈每个观测点单独看都正常，合起来才是故障〉。
        """
        from datetime import datetime, timedelta, timezone
        from unittest import mock

        from app.pipelines import heartbeat_watch as module

        now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
        conn = _HeartbeatConn(now - timedelta(hours=40))
        with mock.patch.dict("os.environ", self._enabled_env()), mock.patch.object(
            module.notify_client, "connect", return_value=conn
        ), mock.patch.object(module.notify_client, "enqueue") as enqueue:
            module.check_heartbeat(now=now, force=True)

        payload = enqueue.call_args.args[0]
        self.assertNotEqual(self.WATCHED, payload["source"])
        self.assertEqual("error", payload["level"])
        self.assertIn(self.WATCHED, payload["summary"])

    def test_never_seen_heartbeat_also_alerts(self) -> None:
        """一次都没来过，和「来过但停了」同样是故障，不能因为查不到就当没事。"""
        from datetime import datetime, timezone
        from unittest import mock

        from app.pipelines import heartbeat_watch as module

        now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
        conn = _HeartbeatConn(None)
        with mock.patch.dict("os.environ", self._enabled_env()), mock.patch.object(
            module.notify_client, "connect", return_value=conn
        ), mock.patch.object(module.notify_client, "enqueue") as enqueue:
            module.check_heartbeat(now=now, force=True)
        enqueue.assert_called_once()

    def test_database_failure_never_escapes(self) -> None:
        """看护绝不能弄挂同步主循环。"""
        from unittest import mock

        from app.pipelines import heartbeat_watch as module

        module.reset_throttle()
        with mock.patch.dict("os.environ", self._enabled_env()), mock.patch.object(
            module.notify_client, "connect", side_effect=RuntimeError("boom")
        ):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertTrue(module.check_heartbeat())
            self.assertIn("心跳看护", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
