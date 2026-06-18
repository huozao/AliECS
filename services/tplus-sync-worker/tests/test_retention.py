import os
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from tplus_datahub.modules.bom.export_bom import export_bom
from tplus_datahub.storage.excel_writer import export_rows_to_excel
from tplus_datahub.storage.retention import (
    DEFAULT_RETENTION_KEEP,
    prune_exports,
    resolve_retention_keep,
)


def _touch_export(directory: Path, name: str, mtime: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


class ResolveRetentionKeepTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("TPLUS_EXPORT_RETENTION", None)

    def tearDown(self):
        os.environ.pop("TPLUS_EXPORT_RETENTION", None)
        if self._saved is not None:
            os.environ["TPLUS_EXPORT_RETENTION"] = self._saved

    def test_explicit_keep_wins_and_floors_at_one(self):
        self.assertEqual(resolve_retention_keep(10), 10)
        self.assertEqual(resolve_retention_keep(0), 1)
        self.assertEqual(resolve_retention_keep(-5), 1)

    def test_env_override(self):
        os.environ["TPLUS_EXPORT_RETENTION"] = "7"
        self.assertEqual(resolve_retention_keep(), 7)

    def test_default_when_unset_or_invalid(self):
        self.assertEqual(resolve_retention_keep(), DEFAULT_RETENTION_KEEP)
        os.environ["TPLUS_EXPORT_RETENTION"] = "not-int"
        self.assertEqual(resolve_retention_keep(), DEFAULT_RETENTION_KEEP)


class PruneExportsTests(unittest.TestCase):
    def test_keeps_newest_n_by_mtime_and_deletes_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            paths = [
                _touch_export(directory, f"bom_2026060{i}_120000.xlsx", mtime=1_000 + i)
                for i in range(5)
            ]

            deleted = prune_exports(directory, "bom", keep=3)

            self.assertEqual(sorted(p.name for p in deleted), ["bom_20260600_120000.xlsx", "bom_20260601_120000.xlsx"])
            survivors = sorted(p.name for p in directory.glob("bom_*.xlsx"))
            self.assertEqual(len(survivors), 3)
            # 当前最新的那个绝不能被删
            self.assertTrue(paths[-1].exists())

    def test_noop_when_at_or_below_keep(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for i in range(3):
                _touch_export(directory, f"bom_2026060{i}_120000.xlsx", mtime=1_000 + i)

            self.assertEqual(prune_exports(directory, "bom", keep=3), [])
            self.assertEqual(len(list(directory.glob("bom_*.xlsx"))), 3)

    def test_scoped_to_prefix_and_standard_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            # 易混前缀：unit 与 unit_group 共存，且有手工放入的非时间戳文件
            for i in range(3):
                _touch_export(directory, f"unit_2026060{i}_120000.xlsx", mtime=2_000 + i)
            keep_group = [
                _touch_export(directory, f"unit_group_2026060{i}_120000.xlsx", mtime=3_000 + i)
                for i in range(3)
            ]
            keep_other = _touch_export(directory, "inventory_20260601_120000.xlsx", mtime=4_000)
            keep_manual = _touch_export(directory, "unit_manual_backup.xlsx", mtime=5_000)

            deleted = prune_exports(directory, "unit", keep=1)

            self.assertEqual(
                sorted(p.name for p in deleted),
                ["unit_20260600_120000.xlsx", "unit_20260601_120000.xlsx"],
            )
            # 其他前缀 / unit_group / 非时间戳手工文件一律不动
            for path in [*keep_group, keep_other, keep_manual]:
                self.assertTrue(path.exists(), f"{path.name} 不应被删除")

    def test_dry_run_lists_without_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for i in range(4):
                _touch_export(directory, f"bom_2026060{i}_120000.xlsx", mtime=1_000 + i)

            planned = prune_exports(directory, "bom", keep=2, dry_run=True)

            self.assertEqual(len(planned), 2)
            # dry-run 不删任何文件
            self.assertEqual(len(list(directory.glob("bom_*.xlsx"))), 4)

    def test_missing_directory_returns_empty(self):
        self.assertEqual(prune_exports(Path("/nonexistent/abc/xyz"), "bom", keep=3), [])

    def test_skips_office_lock_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for i in range(3):
                _touch_export(directory, f"bom_2026060{i}_120000.xlsx", mtime=1_000 + i)
            lock = _touch_export(directory, "~$bom_20260609_120000.xlsx", mtime=9_999)

            deleted = prune_exports(directory, "bom", keep=1)

            self.assertNotIn(lock, deleted)
            self.assertTrue(lock.exists())


def _settings(tmp: str) -> Settings:
    return Settings(
        base_url="https://openapi.example.com",
        app_key="app-key",
        app_secret="app-secret",
        open_token="open-token",
        default_page_size=1,
        timeout_connect=5,
        timeout_read=30,
        output_dir=str(Path(tmp) / "output"),
        data_dir=str(Path(tmp) / "data"),
    )


class ExportPrunesOldFilesTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("TPLUS_EXPORT_RETENTION", None)
        os.environ["TPLUS_EXPORT_RETENTION"] = "2"

    def tearDown(self):
        os.environ.pop("TPLUS_EXPORT_RETENTION", None)
        if self._saved is not None:
            os.environ["TPLUS_EXPORT_RETENTION"] = self._saved

    def test_export_rows_to_excel_prunes_old_same_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp)
            excel_dir = settings.output_root / "excel"
            for i in range(3):
                _touch_export(excel_dir, f"inventory_2026060{i}_120000.xlsx", mtime=1_000 + i)

            target = export_rows_to_excel([{"a": 1}], "inventory", settings.output_root, timestamp="20260609_120000")

            self.assertTrue(target.exists())
            survivors = sorted(p.name for p in excel_dir.glob("inventory_*.xlsx"))
            self.assertEqual(survivors, ["inventory_20260602_120000.xlsx", "inventory_20260609_120000.xlsx"])

    def test_export_bom_prunes_old_bom_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp)
            excel_dir = settings.output_root / "excel"
            for i in range(3):
                _touch_export(excel_dir, f"bom_2026060{i}_120000.xlsx", mtime=1_000 + i)
            rows = [{"Code": "P1", "Name": "父", "BOMChilds": [{"Code": "C1", "Name": "子", "RequiredQuantity": "1"}]}]

            target = export_bom(rows, settings=settings, timestamp="20260609_120000")

            self.assertTrue(target.exists())
            survivors = sorted(p.name for p in excel_dir.glob("bom_*.xlsx"))
            self.assertEqual(survivors, ["bom_20260602_120000.xlsx", "bom_20260609_120000.xlsx"])


if __name__ == "__main__":
    unittest.main()
