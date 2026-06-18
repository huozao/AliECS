import unittest
from unittest.mock import ANY, patch

import tplus_datahub.jobs.job_sync_all as job_sync_all


class JobSyncAllTests(unittest.TestCase):
    def test_main_syncs_verified_base_archives_after_core_modules(self):
        with (
            patch.object(job_sync_all, "load_settings", return_value="settings"),
            patch.object(job_sync_all, "sync_bom", return_value=[]),
            patch.object(job_sync_all, "export_bom", return_value="bom.xlsx"),
            patch.object(job_sync_all, "sync_inventory", return_value=[]),
            patch.object(job_sync_all, "export_inventory", return_value="inventory.xlsx"),
            patch.object(job_sync_all, "sync_partner", return_value=[]),
            patch.object(job_sync_all, "export_partner", return_value="partner.xlsx"),
            patch.object(
                job_sync_all,
                "VERIFIED_BASE_ARCHIVE_QUERY_ENDPOINTS",
                {"warehouse": "/tplus/api/v2/warehouse/Query"},
                create=True,
            ),
            patch.object(job_sync_all, "sync_base_archive", return_value=[{"Code": "01"}], create=True) as sync_archive,
            patch.object(job_sync_all, "export_base_archive", return_value="warehouse.xlsx", create=True) as export_archive,
            patch.object(
                job_sync_all,
                "VERIFIED_VOUCHER_LIST_ENDPOINTS",
                {
                    "sale_order_list": {
                        "endpoint": "/tplus/api/v2/SaleOrderOpenApi/FindVoucherList",
                        "select_fields": ["SaleOrder.ID", "SaleOrder.VoucherDate", "SaleOrder.Code"],
                    }
                },
                create=True,
            ),
            patch.object(job_sync_all, "sync_voucher_list", return_value=[{"code": "SO-001"}], create=True) as sync_voucher,
            patch.object(job_sync_all, "export_voucher_list", return_value="sale_order_list.xlsx", create=True) as export_voucher,
            patch.object(job_sync_all, "sync_purchase_price", return_value=[]),
            patch.object(job_sync_all, "export_purchase_price", return_value="purchase_price.xlsx"),
            patch.object(job_sync_all, "sync_sales_price", return_value=[]),
            patch.object(job_sync_all, "export_sales_price", return_value="sales_price.xlsx"),
        ):
            result = job_sync_all.main()

        self.assertEqual(result, 0)
        sync_archive.assert_called_once_with(
            module_name="warehouse",
            endpoint="/tplus/api/v2/warehouse/Query",
            settings="settings",
            timestamp=ANY,
        )
        export_archive.assert_called_once_with("warehouse", [{"Code": "01"}], settings="settings", timestamp=ANY)
        sync_voucher.assert_called_once_with(
            module_name="sale_order_list",
            endpoint="/tplus/api/v2/SaleOrderOpenApi/FindVoucherList",
            select_fields=["SaleOrder.ID", "SaleOrder.VoucherDate", "SaleOrder.Code"],
            settings="settings",
            timestamp=ANY,
        )
        export_voucher.assert_called_once_with(
            "sale_order_list",
            [{"code": "SO-001"}],
            settings="settings",
            timestamp=ANY,
        )


if __name__ == "__main__":
    unittest.main()
