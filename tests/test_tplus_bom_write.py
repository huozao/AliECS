from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "backend-api" / "app" / "tplus_bom.py"
SPEC = importlib.util.spec_from_file_location("tplus_bom_domain", MODULE_PATH)
assert SPEC and SPEC.loader
main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main)


class TPlusBomWriteDomainTests(unittest.TestCase):
    def _draft(self):
        return (
            {"code": "FG-001", "name": "成品", "unit_name": "个"},
            [
                {"code": "RM-001", "name": "原料一", "unit_name": "公斤", "required_quantity": "2.5"},
                {"code": "RM-002", "name": "原料二", "unit_name": "个", "required_quantity": "1", "warehouse_code": "001"},
            ],
            {"version": "V1", "produce_quantity": "1", "yield_rate": "0.98", "is_default_bom": True},
        )

    def test_build_create_payload_matches_chanjet_contract(self):
        parent, children, options = self._draft()
        payload = main.build_bom_create_payload(parent, children, options)
        self.assertEqual("FG-001", payload["dto"]["Inventory"]["Code"])
        self.assertEqual("V1", payload["dto"]["Version"])
        self.assertEqual("0.98", payload["dto"]["YieldRate"])
        self.assertTrue(payload["dto"]["IsDefaultBom"])
        self.assertEqual("2.5", payload["dto"]["BOMChildDTOs"][0]["RequiredQuantity"])
        self.assertEqual("001", payload["dto"]["BOMChildDTOs"][1]["Warehouse"]["Code"])

    def test_rejects_duplicate_child_and_parent_as_child(self):
        parent, children, options = self._draft()
        children.append(dict(children[0]))
        children.append({"code": "FG-001", "unit_name": "个", "required_quantity": "1"})
        errors = main.validate_bom_draft(parent, children, options)
        self.assertTrue(any("重复" in error for error in errors))
        self.assertTrue(any("父件相同" in error for error in errors))

    def test_rejects_nonpositive_quantity_and_yield_above_one(self):
        parent, children, options = self._draft()
        children[0]["required_quantity"] = "0"
        options["yield_rate"] = "1.01"
        errors = main.validate_bom_draft(parent, children, options)
        self.assertTrue(any("需用数量" in error for error in errors))
        self.assertTrue(any("不超过 1" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
