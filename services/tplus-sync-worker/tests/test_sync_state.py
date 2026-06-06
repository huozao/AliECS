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

    def test_build_snapshot_diff_includes_bom_item_level_changes(self):
        previous = snapshot_bom_rows(
            [
                {
                    "Code": "HYD-4197PC",
                    "Name": "HYD-4197PC珠光红",
                    "Version": "2026-06-03F",
                    "Disabled": "0",
                    "BOMChilds": [
                        {"ID": "1", "Code": "10001024", "Name": "340钛白粉", "RequiredQuantity": 0.04, "Unit": {"Name": "kg"}},
                    ],
                }
            ]
        )
        current = snapshot_bom_rows(
            [
                {
                    "Code": "HYD-4197PC",
                    "Name": "HYD-4197PC珠光红",
                    "Version": "2026-06-03F",
                    "Disabled": "0",
                    "BOMChilds": [
                        {"ID": "1", "Code": "10001024", "Name": "340钛白粉", "RequiredQuantity": 0.05, "Unit": {"Name": "kg"}},
                        {"ID": "2", "Code": "90016", "Name": "L-EG红", "RequiredQuantity": 0.206, "Unit": {"Name": "kg"}},
                    ],
                }
            ]
        )
        previous["id"] = 27
        current["id"] = 28

        diff = build_snapshot_diff(previous=previous, current=current)

        self.assertIsNotNone(diff)
        assert diff is not None
        detail = diff["diff_json"]
        self.assertEqual(1, detail["added_count"])
        self.assertEqual(0, detail["removed_count"])
        self.assertEqual(1, detail["changed_count"])
        self.assertEqual("90016", detail["added"][0]["child_code"])
        self.assertEqual("10001024", detail["changed"][0]["key"]["child_code"])
        self.assertIn("quantity", detail["changed"][0]["changed_fields"])


if __name__ == "__main__":
    unittest.main()
