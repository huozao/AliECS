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

    def test_builds_custom_material_create_payload(self):
        item = {
            "source": "custom", "code": "RM-NEW", "name": "新原料", "specification": "25kg",
            "inventory_class_code": "01", "inventory_class_name": "原材料",
            "unit_code": "1", "unit_name": "kg",
        }
        payload = main.build_inventory_create_payload(item, kind="material")
        dto = payload["dto"]
        self.assertEqual("RM-NEW", dto["Code"])
        self.assertEqual({"Code": "01", "Name": "原材料"}, dto["InventoryClass"])
        self.assertTrue(dto["IsPurchase"])
        self.assertTrue(dto["IsMaterial"])
        self.assertFalse(dto["IsMadeSelf"])
        self.assertEqual({"Code": "01"}, dto["BaseVoucherState"])

    def test_custom_inventory_requires_class_and_unit_codes(self):
        parent, children, options = self._draft()
        children[0].update({"source": "custom", "unit_code": "", "inventory_class_code": ""})
        errors = main.validate_bom_draft(parent, children, options)
        self.assertTrue(any("计量单位编码" in error for error in errors))
        self.assertTrue(any("存货分类编码" in error for error in errors))

    def test_explicit_attributes_override_kind_defaults(self):
        item = {
            "source": "custom", "code": "06000013", "name": "新父件",
            "inventory_class_code": "06", "inventory_class_name": "物料清单",
            "unit_code": "1", "unit_name": "kg",
            "is_purchase": True, "is_sale": True, "is_made_self": True,
            "is_material": True, "is_made_request": True, "is_phantom": False,
        }
        dto = main.build_inventory_create_payload(item, kind="parent")["dto"]
        self.assertTrue(dto["IsPurchase"])
        self.assertTrue(dto["IsMaterial"])
        self.assertTrue(dto["IsMadeRequest"])
        self.assertFalse(dto["IsPhantom"])

    def test_legacy_item_without_attribute_keys_keeps_old_kind_defaults(self):
        item = {
            "source": "custom", "code": "RM-NEW", "name": "新原料",
            "inventory_class_code": "01", "inventory_class_name": "原材料",
            "unit_code": "1", "unit_name": "kg",
        }
        dto = main.build_inventory_create_payload(item, kind="material")["dto"]
        self.assertTrue(dto["IsPurchase"])
        self.assertTrue(dto["IsMaterial"])
        self.assertFalse(dto["IsSale"])
        self.assertFalse(dto["IsMadeSelf"])
        self.assertNotIn("IsMadeRequest", dto)
        self.assertNotIn("IsPhantom", dto)

    def test_all_false_attributes_rejected(self):
        parent = {
            "source": "custom", "code": "06000013", "name": "新父件",
            "inventory_class_code": "06", "inventory_class_name": "物料清单",
            "unit_code": "1", "unit_name": "kg",
            "is_purchase": False, "is_sale": False, "is_made_self": False,
            "is_material": False, "is_made_request": False, "is_phantom": False,
        }
        _, children, options = self._draft()
        errors = main.validate_bom_draft(parent, children, options)
        self.assertTrue(any("至少勾选一项存货属性" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
