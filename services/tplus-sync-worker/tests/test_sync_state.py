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
        self.assertEqual("informational", diff["status"])
        self.assertEqual(1, diff["diff_json"]["row_count_delta"])
        self.assertFalse(diff["diff_json"]["classification"]["needs_review"])

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


    def test_toggling_disabled_is_a_field_change_not_remove_add(self):
        previous = snapshot_bom_rows(
            [{"Code": "P1", "Name": "成品1", "Version": "V1", "Disabled": "0",
              "BOMChilds": [{"ID": "1", "Code": "C1", "Name": "料1", "RequiredQuantity": 1, "Unit": {"Name": "kg"}}]}]
        )
        current = snapshot_bom_rows(
            [{"Code": "P1", "Name": "成品1", "Version": "V1", "Disabled": "1",
              "BOMChilds": [{"ID": "1", "Code": "C1", "Name": "料1", "RequiredQuantity": 1, "Unit": {"Name": "kg"}}]}]
        )
        previous["id"], current["id"] = 1, 2
        diff = build_snapshot_diff(previous=previous, current=current)
        assert diff is not None
        detail = diff["diff_json"]
        self.assertEqual(0, detail["added_count"])
        self.assertEqual(0, detail["removed_count"])
        self.assertEqual(1, detail["changed_count"])
        self.assertIn("disabled", detail["changed"][0]["changed_fields"])


    def _snap(self, rows, sid):
        s = snapshot_bom_rows(rows)
        s["id"] = sid
        return s

    def test_qty_change_needs_review(self):
        prev = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C", "RequiredQuantity": 1}]}], 1)
        cur = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C", "RequiredQuantity": 2}]}], 2)
        diff = build_snapshot_diff(previous=prev, current=cur)
        c = diff["diff_json"]["classification"]
        self.assertEqual(1, c["qty_changed"])
        self.assertTrue(c["needs_review"])
        self.assertEqual("needs_review", diff["status"])

    def test_material_add_needs_review(self):
        prev = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C1", "RequiredQuantity": 1}]}], 1)
        cur = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C1", "RequiredQuantity": 1},
                          {"ID": "2", "Code": "C2", "RequiredQuantity": 1}]}], 2)
        c = build_snapshot_diff(previous=prev, current=cur)["diff_json"]["classification"]
        self.assertEqual(1, c["material_changed"])
        self.assertTrue(c["needs_review"])

    def test_bom_deletion_needs_review(self):
        prev = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C", "RequiredQuantity": 1}]}], 1)
        cur = self._snap([], 2)
        c = build_snapshot_diff(previous=prev, current=cur)["diff_json"]["classification"]
        self.assertEqual(1, c["bom_deleted"])
        self.assertTrue(c["needs_review"])

    def test_status_and_cosmetic_changes_are_informational(self):
        prev = self._snap([{"Code": "P", "Name": "旧名", "Version": "V", "Disabled": "0", "IsDefaultBom": "1",
            "BOMChilds": [{"ID": "1", "Code": "C", "Name": "料", "RequiredQuantity": 1, "Unit": {"Name": "kg"}}]}], 1)
        cur = self._snap([{"Code": "P", "Name": "新名", "Version": "V", "Disabled": "1", "IsDefaultBom": "0",
            "BOMChilds": [{"ID": "1", "Code": "C", "Name": "料改名", "RequiredQuantity": 1, "Unit": {"Name": "g"}}]}], 2)
        diff = build_snapshot_diff(previous=prev, current=cur)
        c = diff["diff_json"]["classification"]
        self.assertEqual(0, c["qty_changed"])
        self.assertEqual(0, c["material_changed"])
        self.assertEqual(0, c["bom_deleted"])
        self.assertFalse(c["needs_review"])
        self.assertEqual("informational", diff["status"])

    def test_new_bom_is_informational(self):
        prev = self._snap([], 1)
        cur = self._snap([{"Code": "P", "Version": "V", "Disabled": "0",
            "BOMChilds": [{"ID": "1", "Code": "C", "RequiredQuantity": 1}]}], 2)
        c = build_snapshot_diff(previous=prev, current=cur)["diff_json"]["classification"]
        self.assertEqual(1, c["bom_added"])
        self.assertFalse(c["needs_review"])


if __name__ == "__main__":
    unittest.main()
