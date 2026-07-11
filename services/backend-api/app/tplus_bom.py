"""T+ BOM 写入领域模型、校验与 OpenAPI 请求构造。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _positive_decimal(value: Any, field: str) -> str:
    raw = _text(value)
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} 必须是数字") from exc
    if number <= 0:
        raise ValueError(f"{field} 必须大于 0")
    return format(number, "f")


def _validate_custom_inventory(item: dict[str, Any], label: str) -> list[str]:
    if _text(item.get("source")) != "custom":
        return []
    errors: list[str] = []
    for key, field in (
        ("name", "名称"),
        ("unit_code", "计量单位编码"),
        ("unit_name", "计量单位名称"),
        ("inventory_class_code", "存货分类编码"),
        ("inventory_class_name", "存货分类名称"),
    ):
        if not _text(item.get(key)):
            errors.append(f"{label}的{field}不能为空")
    return errors


def validate_bom_draft(parent: dict[str, Any], children: list[dict[str, Any]], options: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    parent_code = _text(parent.get("code"))
    if not parent_code:
        errors.append("请选择父件")
    if not _text(parent.get("unit_name")):
        errors.append("父件缺少主计量单位")
    errors.extend(_validate_custom_inventory(parent, "自定义父件"))

    version = _text(options.get("version"))
    if not version:
        errors.append("版本号不能为空")
    try:
        _positive_decimal(options.get("produce_quantity"), "生产数量")
    except ValueError as exc:
        errors.append(str(exc))
    try:
        yield_rate = Decimal(_positive_decimal(options.get("yield_rate"), "成品率"))
        if yield_rate > 1:
            errors.append("成品率必须大于 0 且不超过 1")
    except ValueError as exc:
        errors.append(str(exc))

    if not children:
        errors.append("至少选择一个子件")
    if len(children) > 500:
        errors.append("单个 BOM 最多支持 500 个子件")

    seen: set[str] = set()
    for index, child in enumerate(children, start=1):
        code = _text(child.get("code"))
        if not code:
            errors.append(f"第 {index} 个子件缺少编码")
        elif code == parent_code:
            errors.append(f"第 {index} 个子件不能与父件相同")
        elif code in seen:
            errors.append(f"子件编码 {code} 重复")
        seen.add(code)
        if not _text(child.get("unit_name")):
            errors.append(f"子件 {code or index} 缺少计量单位")
        errors.extend(_validate_custom_inventory(child, f"自定义子件 {code or index}"))
        try:
            _positive_decimal(child.get("required_quantity"), f"子件 {code or index} 的需用数量")
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def build_inventory_create_payload(item: dict[str, Any], *, kind: str) -> dict[str, Any]:
    """构造 T+ 存货创建请求。kind=parent 为自制成品，material 为外购耗用原料。"""
    errors = _validate_custom_inventory(item, "自定义存货")
    if not _text(item.get("code")):
        errors.append("自定义存货编码不能为空")
    if errors:
        raise ValueError("；".join(errors))
    is_parent = kind == "parent"
    dto: dict[str, Any] = {
        "Code": _text(item["code"]),
        "Name": _text(item["name"]),
        "Specification": _text(item.get("specification")),
        "InventoryClass": {
            "Code": _text(item["inventory_class_code"]),
            "Name": _text(item["inventory_class_name"]),
        },
        "Unit": {"Code": _text(item["unit_code"]), "Name": _text(item["unit_name"])},
        "IsSingleUnit": True,
        "UnitType": {"Code": "00"},
        "ValueType": {"Code": "01"},
        "IsPurchase": not is_parent,
        "IsSale": is_parent,
        "IsMadeSelf": is_parent,
        "IsMaterial": not is_parent,
        "BaseVoucherState": {"Code": "01"},
    }
    return {"dto": dto}


def build_custom_inventory_requests(parent: dict[str, Any], children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    if _text(parent.get("source")) == "custom":
        requests.append({"kind": "parent", "code": _text(parent.get("code")), "payload": build_inventory_create_payload(parent, kind="parent")})
    for child in children:
        if _text(child.get("source")) == "custom":
            requests.append({"kind": "material", "code": _text(child.get("code")), "payload": build_inventory_create_payload(child, kind="material")})
    return requests


def build_bom_create_payload(
    parent: dict[str, Any], children: list[dict[str, Any]], options: dict[str, Any]
) -> dict[str, Any]:
    errors = validate_bom_draft(parent, children, options)
    if errors:
        raise ValueError("；".join(errors))

    dto: dict[str, Any] = {
        "Inventory": {"Code": _text(parent["code"])},
        "Unit": {"Name": _text(parent["unit_name"])},
        "Version": _text(options["version"]),
        "ProduceQuantity": _positive_decimal(options["produce_quantity"], "生产数量"),
        "YieldRate": _positive_decimal(options["yield_rate"], "成品率"),
        "IsDefaultBom": bool(options.get("is_default_bom", False)),
        "BOMChildDTOs": [],
    }
    if _text(parent.get("name")):
        dto["Inventory"]["Name"] = _text(parent["name"])
    if _text(options.get("warehouse_code")):
        dto["Warehouse"] = {"Code": _text(options["warehouse_code"])}
    if _text(options.get("routing_code")):
        dto["Routing"] = {"Code": _text(options["routing_code"])}
    if _text(options.get("manufacture_plant_code")):
        dto["Manufactureplant"] = {"Code": _text(options["manufacture_plant_code"])}

    for child in children:
        row: dict[str, Any] = {
            "Inventory": {"Code": _text(child["code"])},
            "Unit": {"Name": _text(child["unit_name"])},
            "RequiredQuantity": _positive_decimal(child["required_quantity"], "需用数量"),
        }
        if _text(child.get("name")):
            row["Inventory"]["Name"] = _text(child["name"])
        if _text(child.get("warehouse_code")):
            row["Warehouse"] = {"Code": _text(child["warehouse_code"])}
        if _text(child.get("child_bom_version")):
            row["ChildBOM"] = {"Version": _text(child["child_bom_version"])}
        dto["BOMChildDTOs"].append(row)
    return {"dto": dto}
