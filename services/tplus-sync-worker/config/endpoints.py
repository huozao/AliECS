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

# 报表通用查询（reportQuery/GetReportData）：价格明细直接取自 T+ 标准报表，
# 一次分页查询即出整表，省去“列表 + 逐单连查”的扇出。请求体须包成 {"request": {...}}；
# 翻页回传 TaskSessionID/SolutionID；data 在 DataSource.Rows（rowType=="D" 为明细行）。
# ReportName/字段映射已对租户逐字段对账确认（见 docs/ops/tplus-price-verify-2026-06-14.md）。
REPORT_QUERY_ENDPOINT = "/tplus/api/v2/reportQuery/GetReportData"

VERIFIED_PRICE_REPORTS = {
    "purchase_price": {
        # 采购到货明细表 == 网页版“采购价格查询”（单据号 PS-，含仓库/供应商/含税单价）
        "report_name": "PU_PurchaseArrivalDetailRpt",
        "date_column": "VoucherDate",
        "columns": [
            ("单据日期", "voucherdate"),
            ("单据编号", "PurchaseArrivalDTOCode"),
            ("供应商编码", "partnerCode"),
            ("供应商", "partnerName"),
            ("部门", "departmentName"),
            ("业务员", "personName"),
            ("仓库", "warehouseName"),
            ("存货编码", "inventoryCode"),
            ("存货", "inventoryName"),
            ("规格型号", "specification"),
            ("计量单位", "unit1Name"),
            ("数量", "quantity"),
            ("报价", "origPrice"),
            ("折扣%", "discountRate"),
            ("单价", "origDiscountPrice"),
            ("金额", "origDiscountAmount"),
            ("含税单价", "origTaxPrice"),
            ("含税金额", "origTaxAmount"),
            ("税额", "origTax"),
        ],
    },
    "sales_price": {
        # 销货单明细表 == 网页版“销售价格查询”（单据号 SA-，含客户/含税单价）
        "report_name": "SA_SaleDeliveryDetailRpt",
        "date_column": "VoucherDate",
        "columns": [
            ("单据日期", "voucherdate"),
            ("单据编号", "saleDeliveryCode"),
            ("客户", "partnerName"),
            ("部门", "departmentName"),
            ("存货编码", "inventoryCode"),
            ("存货", "inventoryName"),
            ("规格型号", "specification"),
            ("计量单位", "unit1Name"),
            ("数量", "quantity"),
            ("报价", "origPrice"),
            ("折扣%", "discountRate"),
            ("单价", "origDiscountPrice"),
            ("金额", "origDiscountAmount"),
            ("含税单价", "origTaxPrice"),
            ("含税金额", "origTaxAmount"),
            ("税额", "origTax"),
        ],
    },
}

# 价格类数值列（字符串→数值）与百分比列（小数→百分数：1.0000→100）
PRICE_NUMERIC_COLUMNS = {"数量", "报价", "单价", "金额", "含税单价", "含税金额", "税额"}
PRICE_PERCENT_COLUMNS = {"折扣%"}

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
