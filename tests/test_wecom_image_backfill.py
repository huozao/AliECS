from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"
sys.path.insert(0, str(WORKER_ROOT))


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


class WorkerImportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._old_sys_path = list(sys.path)
        _clear_app_modules()
        worker_root = str(WORKER_ROOT)
        sys.path[:] = [item for item in sys.path if item != worker_root]
        sys.path.insert(0, worker_root)

    def tearDown(self) -> None:
        _clear_app_modules()
        sys.path[:] = self._old_sys_path


class WeComImageBackfillTests(WorkerImportTestCase):
    def test_parse_sp_no_from_approval_link(self) -> None:
        from app.pipelines.backfill_smartsheet_images import parse_sp_no_from_link

        self.assertEqual(
            "202603240010",
            parse_sp_no_from_link(
                "https://app.work.weixin.qq.com/wework_admin/shenpi_mobile_detail?sp_id=abc&sp_no=202603240010"
            ),
        )
        self.assertEqual("", parse_sp_no_from_link("https://example.com/nope"))

    def test_backfill_decision_requires_empty_image_and_attachment_sp_no(self) -> None:
        from app.pipelines.backfill_smartsheet_images import backfill_candidate

        record = {
            "record_id": "r1",
            "values": {
                "attachment": [
                    {
                        "link": "https://app.work.weixin.qq.com/wework_admin/shenpi_mobile_detail?sp_no=202603240010",
                        "text": "1个附件",
                        "type": "url",
                    }
                ],
                "image": [],
            },
        }

        self.assertEqual(("r1", "202603240010"), backfill_candidate(record, "attachment", "image"))

        record["values"]["image"] = [{"image_url": "https://wdcdn.qpic.cn/existing.jpg"}]
        self.assertIsNone(backfill_candidate(record, "attachment", "image"))

    def test_backfill_decision_accepts_title_keyed_cells(self) -> None:
        from app.pipelines.backfill_smartsheet_images import _field_key_for_write, backfill_candidate

        record = {
            "record_id": "r1",
            "values": {
                "附件": [{"link": "https://example.com/detail?sp_no=202604070003", "text": "1个附件"}],
                "图片": None,
            },
        }

        self.assertEqual(
            ("r1", "202604070003"),
            backfill_candidate(record, "fdNejm", "fUELvC", attachment_title="附件", image_title="图片"),
        )
        self.assertEqual("图片", _field_key_for_write(record["values"], "fUELvC", "图片"))

    def test_extract_images_keeps_only_image_content(self) -> None:
        from app.pipelines.backfill_smartsheet_images import collect_approval_images

        class FakeApproval:
            def __init__(self) -> None:
                self.media_ids: list[str] = []

            def download_media(self, media_id: str) -> bytes:
                self.media_ids.append(media_id)
                if media_id == "pdf-1":
                    return b"%PDF-1.7"
                return b"\xff\xd8\xff\xe0jpeg-bytes"

            def download_url(self, url: str) -> bytes:
                return b"\x89PNG\r\n\x1a\npng-bytes"

        detail = {
            "info": {
                "apply_data": {
                    "contents": [
                        {"control": "File", "value": {"files": [{"file_id": "pdf-1", "file_name": "a.pdf"}, {"file_id": "img-1", "file_name": "a.jpg"}]}},
                        {"control": "Image", "value": ["https://example.com/b.png"]},
                    ]
                }
            }
        }

        images = collect_approval_images(FakeApproval(), detail)

        self.assertEqual(["a.jpg", "b.png"], [item.title for item in images])
        self.assertTrue(all(item.content for item in images))

    def test_run_backfill_updates_rows_and_logs(self) -> None:
        from app.pipelines.backfill_smartsheet_images import run_backfill_images

        class FakeStore:
            def __init__(self) -> None:
                self.logs: list[dict] = []

            def list_image_backfill_targets(self, profiles: list[str]) -> list[dict]:
                return [
                    {
                        "provider": "wecom",
                        "env_profile": "COMPANY_B",
                        "external_doc_id": "doc1",
                        "sheet_title": "配色&样品需求单",
                        "attachment_field_title": "附件",
                        "image_field_title": "图片",
                    }
                ]

            def get_image_backfill_status(self, external_doc_id: str, sheet_id: str, record_id: str) -> str:
                return ""

            def upsert_image_backfill_log(self, **kwargs) -> None:
                self.logs.append(kwargs)

            def close(self) -> None:
                return None

        class FakeSheetClient:
            def __init__(self) -> None:
                self.updated_records: list[dict] = []
                self.uploaded_images: list[tuple[str, bytes]] = []

            def get_sheets(self, docid: str) -> list[dict]:
                return [{"sheet_id": "sheet1", "title": "配色&样品需求单"}]

            def get_fields(self, docid: str, sheet_id: str) -> dict:
                return {
                    "fields": [
                        {"field_id": "f_attach", "field_title": "附件"},
                        {"field_id": "f_image", "field_title": "图片"},
                    ]
                }

            def get_records(self, docid: str, sheet_id: str) -> dict:
                return {
                    "records": [
                        {
                            "record_id": "r1",
                            "values": {
                                "附件": [{"link": "https://example.com/detail?sp_no=202603240010", "text": "1个附件"}],
                                "图片": [],
                            },
                        }
                    ]
                }

            def update_records(self, docid: str, sheet_id: str, records: list[dict]) -> dict:
                self.updated_records.extend(records)
                return {"errcode": 0}

            def upload_image(self, docid: str, content: bytes) -> str:
                self.uploaded_images.append((docid, content))
                return f"https://wdcdn.qpic.cn/{docid}"

        class FakeApprovalClient:
            def get_approval_detail(self, sp_no: str) -> dict:
                return {"info": {"apply_data": {"contents": [{"control": "File", "value": {"files": [{"file_id": "img-1", "file_name": "a.jpg"}]}}]}}}

            def download_media(self, media_id: str) -> bytes:
                return b"\xff\xd8\xff\xe0jpeg-bytes"

            def download_url(self, url: str) -> bytes:
                raise AssertionError("not used")

        store = FakeStore()
        sheet_client = FakeSheetClient()

        result = run_backfill_images(
            profiles_arg="COMPANY_B",
            store=store,
            smartsheet_client_factory=lambda profile: sheet_client,
            approval_client_factory=lambda profile: FakeApprovalClient(),
        )

        self.assertEqual(0, result.exit_code)
        self.assertEqual(1, result.updated_count)
        self.assertEqual("r1", sheet_client.updated_records[0]["record_id"])
        self.assertEqual([("doc1", b"\xff\xd8\xff\xe0jpeg-bytes")], sheet_client.uploaded_images)
        self.assertEqual(
            {"image_url": "https://wdcdn.qpic.cn/doc1", "title": "a.jpg"},
            sheet_client.updated_records[0]["values"]["图片"][0],
        )
        self.assertEqual("done", store.logs[0]["status"])

    def test_default_approval_client_falls_back_to_app_secret(self) -> None:
        from app.pipelines.backfill_smartsheet_images import _default_approval_client

        env = {
            "WECOM_COMPANY_B_CORP_ID": "corp-id",
            "WECOM_COMPANY_B_APP_SECRET": "app-secret",
        }
        with patch.dict("os.environ", env, clear=True):
            client = _default_approval_client("COMPANY_B")

        self.assertEqual("corp-id", client.corpid)
        self.assertEqual("app-secret", client.secret)


if __name__ == "__main__":
    unittest.main()
