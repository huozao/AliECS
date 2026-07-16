from __future__ import annotations

import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "doc-sync-worker"
sys.path.insert(0, str(WORKER_ROOT))


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


class FakeSheetClient:
    def __init__(self, sheets_before: list[dict], default_fields: list[dict]) -> None:
        self._sheets_before = sheets_before
        self._default_fields = default_fields
        self.added_sheet = False
        self.updated_fields: list[dict] = []
        self.added_fields: list[dict] = []
        self.added_records: list[dict] = []

    def get_sheets(self, docid):
        if self.added_sheet or any(s.get("title") == "研发过程记录" for s in self._sheets_before):
            return self._sheets_before + ([{"sheet_id": "rnd1", "title": "研发过程记录"}] if self.added_sheet else [])
        return list(self._sheets_before)

    def add_sheet(self, docid, title):
        self.added_sheet = True
        return {"properties": {"sheet_id": "rnd1"}}

    def get_fields(self, docid, sheet_id):
        return {"fields": self._default_fields}

    def update_fields(self, docid, sheet_id, fields):
        self.updated_fields.extend(fields)
        return {"errcode": 0}

    def add_fields(self, docid, sheet_id, fields):
        self.added_fields.extend(fields)
        return {"errcode": 0}

    def add_records(self, docid, sheet_id, records):
        self.added_records.extend(records)
        return {"errcode": 0}


class RndWriterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _clear_app_modules()

    def tearDown(self) -> None:
        _clear_app_modules()

    def _mod(self):
        worker_path = str(WORKER_ROOT)
        while worker_path in sys.path:
            sys.path.remove(worker_path)
        sys.path.insert(0, worker_path)
        import app.pipelines.rnd_record_writer as mod

        return mod

    def test_build_node_row_values(self) -> None:
        mod = self._mod()
        msg = {"created_at": None, "from_userid": "WangHao", "node_category": "打样",
               "node_summary": "第一版完成", "text_content": "x"}
        v = mod.build_node_row_values(msg, "202606250001")
        self.assertEqual("WangHao", v["发言人"][0]["text"])
        self.assertEqual("打样", v["节点类型"][0]["text"])
        self.assertEqual("第一版完成", v["内容"][0]["text"])
        self.assertEqual("202606250001", v["审批单编号"][0]["text"])
        self.assertNotIn("图片", v)
        v2 = mod.build_node_row_values(msg, "k", image_url="https://wdcdn.qpic.cn/x")
        self.assertEqual("https://wdcdn.qpic.cn/x", v2["图片"][0]["image_url"])

    def test_quote_image_urls(self) -> None:
        mod = self._mod()
        self.assertEqual(["http://a/x.jpg"], mod._quote_image_urls({"msgtype": "image", "image": {"url": "http://a/x.jpg"}}))
        self.assertEqual([], mod._quote_image_urls({"msgtype": "text", "text": {"content": "hi"}}))
        self.assertEqual([], mod._quote_image_urls({}))

    def test_local_image_bytes_only_reads_configured_root(self) -> None:
        mod = self._mod()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "node.jpg"
            image.write_bytes(b"image-bytes")
            outside = root.parent / "outside-node.jpg"
            outside.write_bytes(b"outside")
            try:
                with patch.dict("os.environ", {"WECOM_GROUP_MEDIA_DIR": temp_dir}):
                    self.assertEqual([b"image-bytes"], mod._local_image_bytes([str(image), str(outside)]))
            finally:
                outside.unlink(missing_ok=True)

    def test_ensure_rnd_sheet_existing(self) -> None:
        mod = self._mod()
        client = FakeSheetClient([{"sheet_id": "rnd1", "title": "研发过程记录"}], [])
        self.assertEqual("rnd1", mod.ensure_rnd_sheet(client, "doc1"))
        self.assertFalse(client.added_sheet)

    def test_ensure_rnd_sheet_creates(self) -> None:
        mod = self._mod()
        client = FakeSheetClient([{"sheet_id": "s0", "title": "配色&样品需求单"}], [{"field_id": "f0", "field_title": "默认"}])
        sid = mod.ensure_rnd_sheet(client, "doc1")
        self.assertEqual("rnd1", sid)
        self.assertTrue(client.added_sheet)
        # 默认字段被重命名为「时间」
        self.assertEqual("时间", client.updated_fields[0]["field_title"])
        # 其余字段被追加（不含「时间」）
        titles = [f["field_title"] for f in client.added_fields]
        self.assertIn("图片", titles)
        self.assertNotIn("时间", titles)


if __name__ == "__main__":
    unittest.main()
