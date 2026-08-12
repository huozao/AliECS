from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
