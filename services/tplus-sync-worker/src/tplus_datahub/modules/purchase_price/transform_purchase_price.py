from __future__ import annotations

from typing import Any

from tplus_datahub.modules.price_common import (
    PURCHASE_PRICE_COLUMNS,
    code,
    display,
    iter_details,
    number,
    pick,
    row_for_columns,
    unwrap_dto,
)


def transform_purchase_price_rows(dtos: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dto in dtos:
        document = unwrap_dto(dto)
        partner = pick(document, "Partner", "Supplier", "Vendor")
        department = pick(document, "Department")
        clerk = pick(document, "Clerk", "Person", "Employee")
        header_warehouse = pick(document, "Warehouse")
        header_project = pick(document, "Project")

        for detail in iter_details(document):
            warehouse = pick(detail, "Warehouse") or header_warehouse
            project = pick(detail, "Project") or header_project
            values = {
                "单据日期": pick(document, "VoucherDate", "Date", "BusinessDate"),
                "单据编号": pick(document, "Code", "VoucherCode", "No"),
                "供应商编码": code(partner) or pick(document, "PartnerCode", "SupplierCode"),
                "供应商": display(partner) or pick(document, "PartnerName", "SupplierName"),
                "供应商简称": pick(partner, "ShortName", "shortName") if isinstance(partner, dict) else None,
                "部门": display(department) or pick(document, "DepartmentName"),
                "业务员": display(clerk) or pick(document, "ClerkName", "PersonName"),
                "仓库": display(warehouse) or pick(detail, "WarehouseName"),
                "项目": display(project) or pick(detail, "ProjectName"),
                "存货编码": pick(detail, "InventoryCode", "Inventory.Code", "Inventory.code"),
                "存货": pick(detail, "InventoryName", "Inventory.Name", "Inventory.name"),
                "规格型号": pick(detail, "Specification", "Inventory.Specification", "Inventory.InvSpecification"),
                "计量单位": display(pick(detail, "Unit", "UnitDTO")) or pick(detail, "UnitName"),
                "数量": number(pick(detail, "Quantity", "OrigQuantity", "BaseQuantity")),
                "折扣%": number(pick(detail, "Discount", "DiscountRate", "DiscountPercent")),
                "单价": number(pick(detail, "OrigDiscountPrice", "DiscountPrice", "Price")),
                "金额": number(pick(detail, "OrigDiscountAmount", "DiscountAmount", "Amount")),
                "税率%": number(pick(detail, "TaxRate", "TaxRatePercent")),
                "含税单价": number(pick(detail, "OrigTaxPrice", "TaxPrice")),
                "含税金额": number(pick(detail, "OrigTaxAmount", "TaxAmountTotal", "TaxInclusiveAmount")),
                "税额": number(pick(detail, "TaxAmount", "Tax")),
            }
            rows.append(row_for_columns(PURCHASE_PRICE_COLUMNS, values))
    return rows
