import unittest
from unittest.mock import ANY, patch

import tplus_datahub.jobs.job_sync_bom as job_sync_bom


class JobSyncBomTests(unittest.TestCase):
    def test_build_query_params_from_target_uses_parent_code_and_version(self):
        params = job_sync_bom.build_query_params_from_target(
            {"parent_code": "HYD-4197PC", "version": "2026-06-03F"}
        )

        self.assertEqual({"Code": "HYD-4197PC", "Version": "2026-06-03F"}, params)

    def test_incremental_request_syncs_target_with_disabled_scope(self):
        with (
            patch.object(job_sync_bom, "load_settings", return_value="settings"),
            patch.object(job_sync_bom, "sync_bom", return_value=[{"Code": "HYD-4197PC"}]) as sync_bom,
            patch.object(job_sync_bom, "export_bom", return_value="bom.xlsx") as export_bom,
        ):
            result = job_sync_bom.main(
                target={"parent_code": "HYD-4197PC", "version": "2026-06-03F"},
                mode="incremental",
            )

        self.assertEqual(0, result)
        sync_bom.assert_called_once_with(
            settings="settings",
            timestamp=ANY,
            query_params={"Code": "HYD-4197PC", "Version": "2026-06-03F"},
            include_disabled=True,
        )
        export_bom.assert_called_once_with([{"Code": "HYD-4197PC"}], settings="settings", timestamp=ANY)

    def test_incremental_request_without_target_falls_back_to_full_bom(self):
        with (
            patch.object(job_sync_bom, "load_settings", return_value="settings"),
            patch.object(job_sync_bom, "sync_bom", return_value=[]) as sync_bom,
            patch.object(job_sync_bom, "export_bom", return_value="bom.xlsx"),
        ):
            result = job_sync_bom.main(target={}, mode="incremental")

        self.assertEqual(0, result)
        sync_bom.assert_called_once_with(settings="settings", timestamp=ANY)


if __name__ == "__main__":
    unittest.main()
