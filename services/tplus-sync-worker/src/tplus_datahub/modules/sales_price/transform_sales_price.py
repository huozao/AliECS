from __future__ import annotations

from typing import Any

from tplus_datahub.modules.price_common import (
    SALES_PRICE_COLUMNS,
    display,
    iter_details,
    number,
    pick,
    row_for_columns,
    unwrap_dto,
)


def transform_sales_price_rows(dtos: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dto in dtos:
        document = unwrap_dto(dto)
        customer = pick(document, "Partner", "Customer")
        department = pick(document, "Department")
        clerk = pick(document, "Clerk", "Person", "Employee")

        for detail in iter_details(document):
            values = {
                "单据日期": pick(document, "VoucherDate", "Date", "BusinessDate"),
                "单据编号": pick(document, "Code", "VoucherCode", "No"),
                "客户": display(customer) or pick(document, "PartnerName", "CustomerName"),
                "部门": display(department) or pick(document, "DepartmentName"),
                "业务员": display(clerk) or pick(document, "ClerkName", "PersonName"),
                "存货编码": pick(detail, "InventoryCode", "Inventory.Code", "Inventory.code"),
                "存货": pick(detail, "InventoryName", "Inventory.Name", "Inventory.name"),
                "规格型号": pick(detail, "Specification", "Inventory.Specification", "Inventory.InvSpecification"),
                "计量单位": display(pick(detail, "Unit", "UnitDTO")) or pick(detail, "UnitName"),
                "数量": number(pick(detail, "Quantity", "OrigQuantity", "BaseQuantity")),
                "折扣%": number(pick(detail, "Discount", "DiscountRate", "DiscountPercent")),
                "单价": number(pick(detail, "OrigDiscountPrice", "DiscountPrice", "Price")),
                "金额": number(pick(detail, "OrigDiscountAmount", "DiscountAmount", "Amount")),
                "含税单价": number(pick(detail, "OrigTaxPrice", "TaxPrice")),
                "含税金额": number(pick(detail, "OrigTaxAmount", "TaxAmountTotal", "TaxInclusiveAmount")),
                "税额": number(pick(detail, "TaxAmount", "Tax")),
            }
            rows.append(row_for_columns(SALES_PRICE_COLUMNS, values))
    return rows
