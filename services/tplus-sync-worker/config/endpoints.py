BOM_QUERY_PAGE = "/tplus/api/v2/bom/QueryPage"
INVENTORY_QUERY_PAGE = "/tplus/api/v2/inventory/QueryPage"
PARTNER_QUERY_PAGE = "/tplus/api/v2/partner/QueryPage"

VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS = {
    # 现存量（仓库×存货粒度，含 ExistingQuantity/AvailableQuantity；2026-06-10 实测 384 行，偶尔响应 >30s 需调大 REQUEST_TIMEOUT_READ）
    "current_stock": "/tplus/api/v2/currentStock/Query",
    "warehouse": "/tplus/api/v2/warehouse/Query",
    "unit_group": "/tplus/api/v2/UnitGroup/Query",
    "unit": "/tplus/api/v2/Unit/Query",
    "project": "/tplus/api/v2/Project/Query2",
    "project_class": "/tplus/api/v2/ProjectClass/Query",
    "brand": "/tplus/api/v2/brand/Query",
    "district": "/tplus/api/v2/district/Query",
}

VERIFIED_VOUCHER_LIST_ENDPOINTS = {
    "sale_order_list": {
        "endpoint": "/tplus/api/v2/SaleOrderOpenApi/FindVoucherList",
        "select_fields": ["SaleOrder.ID", "SaleOrder.VoucherDate", "SaleOrder.Code"],
    },
    "sale_delivery_list": {
        "endpoint": "/tplus/api/v2/SaleDeliveryOpenApi/FindVoucherList",
        "select_fields": ["SaleDelivery.ID", "SaleDelivery.VoucherDate", "SaleDelivery.Code"],
    },
    "purchase_order_list": {
        "endpoint": "/tplus/api/v2/PurchaseOrderOpenApi/FindVoucherList",
        "select_fields": ["PurchaseOrder.ID", "PurchaseOrder.VoucherDate", "PurchaseOrder.Code"],
    },
    "purchase_arrival_list": {
        "endpoint": "/tplus/api/v2/PurchaseArrivalOpenApi/FindVoucherList",
        "select_fields": ["PurchaseArrival.ID", "PurchaseArrival.VoucherDate", "PurchaseArrival.Code"],
    },
    "purchase_receive_list": {
        "endpoint": "/tplus/api/v2/PurchaseReceiveOpenApi/FindVoucherList",
        "select_fields": ["RDRecord.ID", "RDRecord.VoucherDate", "RDRecord.Code"],
    },
    "material_dispatch_list": {
        "endpoint": "/tplus/api/v2/MaterialDispatchOpenApi/FindVoucherList",
        "select_fields": ["RDRecord.ID", "RDRecord.VoucherDate", "RDRecord.Code"],
    },
}

VERIFIED_PRICE_ENDPOINTS = {
    "purchase_price": {
        "list_endpoint": "/tplus/api/v2/PurchaseArrivalOpenApi/FindVoucherList",
        "detail_endpoint": "/tplus/api/v2/PurchaseArrivalOpenApi/GetVoucherDTO",
        "select_fields": ["PurchaseArrival.ID", "PurchaseArrival.VoucherDate", "PurchaseArrival.Code"],
        "note": "到货单明细价字段需按当前 T+ 版本文档和实时验证结果复核。",
    },
    "sales_price": {
        "list_endpoint": "/tplus/api/v2/SaleDeliveryOpenApi/FindVoucherList",
        "detail_endpoint": "/tplus/api/v2/SaleDeliveryOpenApi/GetVoucherDTO",
        "select_fields": ["SaleDelivery.ID", "SaleDelivery.VoucherDate", "SaleDelivery.Code"],
        "note": "销货单明细价字段需按当前 T+ 版本文档和实时验证结果复核。",
    },
}

PENDING_ENDPOINTS = {
    "material": "pending",
    "product": "pending",
    "cost": "pending",
    "department": "returned_999_on_empty_query",
    "person": "returned_999_on_empty_query",
    "marketing_organ": "returned_999_on_empty_query",
    "settle_style": "returned_999_on_empty_query",
    "bank_account": "returned_999_on_empty_query",
    "currency": "returned_999_on_empty_query",
    "expense": "returned_999_on_empty_query",
    "income": "returned_999_on_empty_query",
    "supplier": "covered_by_partner_pending_split",
    "customer": "covered_by_partner_pending_split",
    "sales": "pending",
    "purchase": "pending",
}
