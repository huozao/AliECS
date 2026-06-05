import unittest

from tplus_datahub.jobs.sync_state import build_snapshot_diff, snapshot_bom_rows


class SyncStateTests(unittest.TestCase):
    def test_snapshot_bom_rows_is_order_independent(self):
        left = snapshot_bom_rows(
            [
                {"Code": "B", "Version": "V1", "BOMChilds": [{"Code": "C2"}]},
                {"Code": "A", "Version": "V1", "BOMChilds": [{"Code": "C1"}]},
            ]
        )
        right = snapshot_bom_rows(
            [
                {"Code": "A", "Version": "V1", "BOMChilds": [{"Code": "C1"}]},
                {"Code": "B", "Version": "V1", "BOMChilds": [{"Code": "C2"}]},
            ]
        )

        self.assertEqual(2, left["row_count"])
        self.assertEqual(left["snapshot_hash"], right["snapshot_hash"])

    def test_build_snapshot_diff_returns_none_for_identical_snapshots(self):
        snapshot = {"id": 1, "row_count": 2, "snapshot_hash": "abc"}

        self.assertIsNone(build_snapshot_diff(previous=snapshot, current=snapshot))

    def test_build_snapshot_diff_summarizes_changed_snapshot(self):
        diff = build_snapshot_diff(
            previous={"id": 1, "row_count": 2, "snapshot_hash": "abc"},
            current={"id": 2, "row_count": 3, "snapshot_hash": "def"},
        )

        self.assertIsNotNone(diff)
        assert diff is not None
        self.assertEqual("needs_review", diff["status"])
        self.assertIn("BOM full snapshot changed", diff["summary"])
        self.assertEqual(1, diff["diff_json"]["row_count_delta"])


if __name__ == "__main__":
    unittest.main()
