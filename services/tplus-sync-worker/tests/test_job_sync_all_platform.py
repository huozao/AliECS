import unittest
from contextlib import ExitStack
from unittest.mock import patch

import tplus_datahub.jobs.job_sync_all as job_sync_all
import tplus_datahub.jobs.sync_state as sync_state
from config.settings import ConfigError
from tplus_datahub.core.exceptions import ChanjetAPIError
from tplus_datahub.jobs.sync_state import FullBomSnapshotResult


class RecordingPlatform:
    def __init__(self):
        self.started = []
        self.steps = []
        self.finished = []

    def start_run(self, **kwargs):
        self.started.append(kwargs)
        return 77

    def upsert_step(self, run_id, seq, name, status, items=0, message=""):
        self.steps.append(
            {
                "run_id": run_id,
                "seq": seq,
                "name": name,
                "status": status,
                "items": items,
                "message": message,
            }
        )

    def finish_run(self, run_id, **kwargs):
        self.finished.append({"run_id": run_id, **kwargs})

    def terminal_step_names(self, status):
        return [step["name"] for step in self.steps if step["status"] == status]


class RaisingPlatform:
    def start_run(self, **_kwargs):
        raise RuntimeError("platform unavailable")

    def upsert_step(self, *_args, **_kwargs):
        raise RuntimeError("platform unavailable")

    def finish_run(self, *_args, **_kwargs):
        raise RuntimeError("platform unavailable")


class RaisingAfterStartPlatform(RaisingPlatform):
    def start_run(self, **_kwargs):
        return 77


class JobSyncAllPlatformTests(unittest.TestCase):
    def _patch_modules(self, **overrides):
        defaults = {
            "load_settings": patch.object(job_sync_all, "load_settings", return_value="settings"),
            "sync_bom": patch.object(job_sync_all, "sync_bom", return_value=[]),
            "upsert_and_snapshot_full_bom": patch.object(
                job_sync_all,
                "upsert_and_snapshot_full_bom",
                return_value=FullBomSnapshotResult(
                    full_rows=[], full_snapshot_id=19, diff_summary={"needs_review": False}
                ),
            ),
            "export_bom": patch.object(job_sync_all, "export_bom", return_value="bom.xlsx"),
            "sync_inventory": patch.object(job_sync_all, "sync_inventory", return_value=[]),
            "export_inventory": patch.object(job_sync_all, "export_inventory", return_value="inventory.xlsx"),
            "persist_inventory_records": patch.object(job_sync_all, "persist_inventory_records"),
            "sync_partner": patch.object(job_sync_all, "sync_partner", return_value=[]),
            "export_partner": patch.object(job_sync_all, "export_partner", return_value="partner.xlsx"),
            "VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS": patch.object(
                job_sync_all,
                "VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS",
                {"warehouse": "/tplus/api/v2/warehouse/Query"},
            ),
            "VERIFIED_VOUCHER_LIST_ENDPOINTS": patch.object(
                job_sync_all, "VERIFIED_VOUCHER_LIST_ENDPOINTS", {}
            ),
            "sync_base_archive": patch.object(job_sync_all, "sync_base_archive", return_value=[]),
            "export_base_archive": patch.object(
                job_sync_all, "export_base_archive", return_value="warehouse.xlsx"
            ),
            "sync_purchase_price": patch.object(job_sync_all, "sync_purchase_price", return_value=[]),
            "export_purchase_price": patch.object(
                job_sync_all, "export_purchase_price", return_value="purchase_price.xlsx"
            ),
            "sync_sales_price": patch.object(job_sync_all, "sync_sales_price", return_value=[]),
            "export_sales_price": patch.object(
                job_sync_all, "export_sales_price", return_value="sales_price.xlsx"
            ),
        }
        defaults.update(overrides)
        return defaults.values()

    def _run(self, platform, **overrides):
        with ExitStack() as stack:
            for context in self._patch_modules(**overrides):
                stack.enter_context(context)
            return job_sync_all.run(trigger="schedule", platform=platform)

    def test_sync_all_records_each_actual_module_and_partial_result(self):
        platform = RecordingPlatform()
        failure_message = "Authorization: Bearer secret-value " + "x" * 600
        failure = RuntimeError(failure_message)

        result = self._run(
            platform,
            sync_bom=patch.object(job_sync_all, "sync_bom", return_value=[{"ID": 1}, {"ID": 2}]),
            upsert_and_snapshot_full_bom=patch.object(
                job_sync_all,
                "upsert_and_snapshot_full_bom",
                return_value=FullBomSnapshotResult(
                    full_rows=[{"ID": index} for index in range(5)],
                    full_snapshot_id=19,
                    diff_summary={"needs_review": False},
                ),
            ),
            sync_inventory=patch.object(
                job_sync_all, "sync_inventory", return_value=[{"Code": "1"}, {"Code": "2"}, {"Code": "3"}]
            ),
            export_inventory=patch.object(job_sync_all, "export_inventory", side_effect=failure),
            sync_partner=patch.object(job_sync_all, "sync_partner", return_value=[{"Code": "P1"}]),
            sync_base_archive=patch.object(
                job_sync_all, "sync_base_archive", return_value=[{"Code": str(index)} for index in range(4)]
            ),
            VERIFIED_VOUCHER_LIST_ENDPOINTS=patch.object(
                job_sync_all,
                "VERIFIED_VOUCHER_LIST_ENDPOINTS",
                {"sale_order_list": {"endpoint": "/voucher", "select_fields": ["Code"]}},
            ),
            sync_voucher_list=patch.object(
                job_sync_all, "sync_voucher_list", return_value=[{"Code": "S1"}, {"Code": "S2"}]
            ),
            export_voucher_list=patch.object(
                job_sync_all, "export_voucher_list", return_value="sale_order_list.xlsx"
            ),
            sync_purchase_price=patch.object(
                job_sync_all, "sync_purchase_price", return_value=[{"Code": "B1"}, {"Code": "B2"}]
            ),
            sync_sales_price=patch.object(job_sync_all, "sync_sales_price", return_value=[{"Code": "C1"}]),
        )

        self.assertEqual("chanjet.full", platform.started[0]["job_key"])
        self.assertEqual("schedule", platform.started[0]["trigger"])
        self.assertEqual(77, result.platform_run_id)
        expected = [
            (1, "bom", "success", 2),
            (2, "inventory", "failed", 3),
            (3, "partner", "success", 1),
            (4, "warehouse", "success", 4),
            (5, "sale_order_list", "success", 2),
            (6, "purchase_price", "success", 2),
            (7, "sales_price", "success", 1),
        ]
        self.assertEqual(14, len(platform.steps))
        for seq, name, terminal_status, items in expected:
            module_steps = [step for step in platform.steps if step["name"] == name]
            self.assertEqual(2, len(module_steps), name)
            self.assertEqual([seq, seq], [step["seq"] for step in module_steps], name)
            self.assertEqual(["running", terminal_status], [step["status"] for step in module_steps], name)
            self.assertEqual([0, items], [step["items"] for step in module_steps], name)
        failed_message = next(
            step["message"] for step in platform.steps
            if step["name"] == "inventory" and step["status"] == "failed"
        )
        self.assertEqual(("Authorization: [REDACTED] " + "x" * 600)[:500], failed_message)
        self.assertEqual(500, len(failed_message))
        self.assertEqual("partial", platform.finished[0]["status"])
        self.assertEqual(
            {
                "export_files": result.export_files,
                "diff_summary": result.diff_summary,
                "full_snapshot_id": result.full_snapshot_id,
                "failed_modules": ["inventory"],
            },
            platform.finished[0]["detail_json"],
        )

    def test_platform_failure_does_not_change_sync_result_or_outputs(self):
        result = self._run(RaisingPlatform())

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            ["bom.xlsx", "inventory.xlsx", "partner.xlsx", "warehouse.xlsx", "purchase_price.xlsx", "sales_price.xlsx"],
            result.export_files,
        )
        self.assertIsNone(result.platform_run_id)

    def test_failed_fetch_records_zero_items_when_no_count_is_reliable(self):
        platform = RecordingPlatform()

        self._run(
            platform,
            sync_inventory=patch.object(job_sync_all, "sync_inventory", side_effect=RuntimeError("fetch failed")),
        )

        failed_step = next(
            step for step in platform.steps
            if step["name"] == "inventory" and step["status"] == "failed"
        )
        self.assertEqual(0, failed_step["items"])

    def test_step_and_finish_platform_failures_do_not_change_sync_result(self):
        result = self._run(RaisingAfterStartPlatform())

        self.assertEqual(0, result.exit_code)
        self.assertEqual(77, result.platform_run_id)
        self.assertEqual(6, len(result.export_files))

    def test_config_error_finishes_platform_as_failed_and_preserves_legacy_result(self):
        platform = RecordingPlatform()
        error = ConfigError("Authorization=secret-value")

        with patch.object(job_sync_all, "load_settings", side_effect=error):
            result = job_sync_all.run(platform=platform)

        self.assertEqual(2, result.exit_code)
        self.assertEqual(["config"], result.failed_modules)
        self.assertEqual("failed", platform.finished[0]["status"])
        self.assertNotIn(
            "secret-value",
            job_sync_all.sync_job_platform.safe_error_message(platform.finished[0]["error"]),
        )

    def test_unexpected_top_level_error_finishes_platform_then_propagates(self):
        platform = RecordingPlatform()

        with patch.object(job_sync_all, "load_settings", side_effect=RuntimeError("unexpected")):
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                job_sync_all.run(platform=platform)

        self.assertEqual("failed", platform.finished[0]["status"])


class _RunCursor:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _sql, _params):
        self.events.append("insert")

    def fetchone(self):
        return (41,)


class _RunConnection:
    def __init__(self, events, commit_error=None):
        self.events = events
        self.commit_error = commit_error

    def cursor(self):
        return _RunCursor(self.events)

    def commit(self):
        self.events.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    def close(self):
        self.events.append("close")


class RecordLegacyRunTests(unittest.TestCase):
    def test_scheduled_legacy_run_commits_before_platform_attach(self):
        events = []
        conn = _RunConnection(events)
        fake_psycopg = type("Psycopg", (), {"connect": staticmethod(lambda *_args, **_kwargs: conn)})

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://example"}),
            patch.object(sync_state, "psycopg", fake_psycopg),
            patch.object(
                sync_state,
                "attach_legacy_ref",
                side_effect=lambda platform_id, legacy_id: events.append(("attach", platform_id, legacy_id)),
                create=True,
            ),
        ):
            run_id = sync_state.record_tplus_sync_run_if_configured(
                module="all",
                mode="scheduled_full",
                status="success",
                platform_run_id=77,
            )

        self.assertEqual(41, run_id)
        self.assertLess(events.index("commit"), events.index(("attach", 77, 41)))

    def test_platform_attach_failure_does_not_change_legacy_result(self):
        events = []
        conn = _RunConnection(events)
        fake_psycopg = type("Psycopg", (), {"connect": staticmethod(lambda *_args, **_kwargs: conn)})

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://example"}),
            patch.object(sync_state, "psycopg", fake_psycopg),
            patch.object(
                sync_state,
                "attach_legacy_ref",
                side_effect=RuntimeError("platform down"),
                create=True,
            ) as attach,
        ):
            run_id = sync_state.record_tplus_sync_run_if_configured(
                module="all",
                mode="scheduled_full",
                status="success",
                platform_run_id=77,
            )

        self.assertEqual(41, run_id)
        self.assertIn("commit", events)
        attach.assert_called_once_with(77, 41)

    def test_scheduled_commit_failure_never_attaches_platform_run(self):
        events = []
        conn = _RunConnection(events, commit_error=RuntimeError("commit failed"))
        fake_psycopg = type("Psycopg", (), {"connect": staticmethod(lambda *_args, **_kwargs: conn)})

        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://example"}),
            patch.object(sync_state, "psycopg", fake_psycopg),
            patch.object(sync_state, "attach_legacy_ref", create=True) as attach,
        ):
            run_id = sync_state.record_tplus_sync_run_if_configured(
                module="all",
                mode="scheduled_full",
                status="success",
                platform_run_id=77,
            )

        self.assertIsNone(run_id)
        attach.assert_not_called()


if __name__ == "__main__":
    unittest.main()
