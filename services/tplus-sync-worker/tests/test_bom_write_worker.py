from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tplus_datahub.core.exceptions import ChanjetAPIError
from tplus_datahub.jobs import bom_write_worker


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, endpoint, payload):
        self.calls.append((endpoint, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BomWriteWorkerTests(unittest.TestCase):
    def _submission(self):
        return {
            "id": 7,
            "request_json": {
                "dto": {
                    "Inventory": {"Code": "FG-001"},
                    "Version": "V1",
                    "Unit": {"Name": "个"},
                    "ProduceQuantity": "1",
                    "YieldRate": "1",
                    "BOMChildDTOs": [],
                }
            },
        }

    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_create_then_query_verifies_exact_bom(self, finish, _event):
        client = FakeClient([
            [],
            {"result": 123},
            [{"ID": 123, "Code": "FG-001", "Version": "V1"}],
        ])
        status = bom_write_worker.process_submission(self._submission(), client=client)
        self.assertEqual("success", status)
        self.assertEqual(bom_write_worker.BOM_QUERY_ENDPOINT, client.calls[0][0])
        self.assertEqual(bom_write_worker.BOM_CREATE_ENDPOINT, client.calls[1][0])
        self.assertEqual(bom_write_worker.BOM_QUERY_ENDPOINT, client.calls[2][0])
        self.assertEqual("success", finish.call_args.kwargs["status"])
        self.assertEqual("123", finish.call_args.kwargs["result_bom_id"])

    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_timeout_becomes_needs_review_without_retry(self, finish, _event):
        client = FakeClient([[], TimeoutError("timeout")])
        status = bom_write_worker.process_submission(self._submission(), client=client)
        self.assertEqual("needs_review", status)
        self.assertEqual(2, len(client.calls))
        self.assertEqual("needs_review", finish.call_args.kwargs["status"])

    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_existing_parent_version_is_rejected_before_create(self, finish, _event):
        client = FakeClient([[{"ID": 88, "Code": "FG-001", "Version": "V1"}]])
        status = bom_write_worker.process_submission(self._submission(), client=client)
        self.assertEqual("failed", status)
        self.assertEqual(1, len(client.calls))
        self.assertIn("已存在", finish.call_args.kwargs["error"]["message"])

    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_custom_inventory_is_created_and_verified_before_bom(self, finish, _event):
        submission = self._submission()
        submission["request_json"] = {
            "bom": submission["request_json"],
            "custom_inventories": [{
                "kind": "material", "code": "RM-NEW",
                "payload": {"dto": {"Code": "RM-NEW", "Name": "新原料"}},
            }],
        }
        client = FakeClient([
            [],
            [],
            {"result": 55},
            [{"ID": 55, "Code": "RM-NEW", "Name": "新原料"}],
            {"result": 123},
            [{"ID": 123, "Code": "FG-001", "Version": "V1"}],
        ])
        status = bom_write_worker.process_submission(submission, client=client)
        self.assertEqual("success", status)
        self.assertEqual(
            [
                bom_write_worker.BOM_QUERY_ENDPOINT,
                bom_write_worker.INVENTORY_QUERY_ENDPOINT,
                bom_write_worker.INVENTORY_CREATE_ENDPOINT,
                bom_write_worker.INVENTORY_QUERY_ENDPOINT,
                bom_write_worker.BOM_CREATE_ENDPOINT,
                bom_write_worker.BOM_QUERY_ENDPOINT,
            ],
            [call[0] for call in client.calls],
        )
        self.assertEqual("success", finish.call_args.kwargs["status"])

    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_custom_inventory_code_conflict_stops_before_create(self, finish, _event):
        submission = self._submission()
        submission["request_json"] = {
            "bom": submission["request_json"],
            "custom_inventories": [{
                "kind": "material", "code": "RM-NEW",
                "payload": {"dto": {"Code": "RM-NEW", "Name": "新原料"}},
            }],
        }
        client = FakeClient([[], [{"Code": "RM-NEW", "Name": "已有别名"}]])
        status = bom_write_worker.process_submission(submission, client=client)
        self.assertEqual("failed", status)
        self.assertEqual(2, len(client.calls))
        self.assertIn("名称为", finish.call_args.kwargs["error"]["message"])

    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_scalar_create_response_verified_by_query_is_success(self, finish, _event):
        # 生产实锤：T+ Create 成功返回裸标量 ID（非 {result:...} 包裹）
        client = FakeClient([
            [],
            123,
            [{"ID": 123, "Code": "FG-001", "Version": "V1"}],
        ])
        status = bom_write_worker.process_submission(self._submission(), client=client)
        self.assertEqual("success", status)
        self.assertEqual("123", finish.call_args.kwargs["result_bom_id"])

    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_custom_inventory_scalar_create_response_is_accepted(self, finish, _event):
        submission = self._submission()
        submission["request_json"] = {
            "bom": submission["request_json"],
            "custom_inventories": [{
                "kind": "parent", "code": "06000001",
                "payload": {"dto": {"Code": "06000001", "Name": "新父件"}},
            }],
        }
        client = FakeClient([
            [],
            None,
            568,
            [{"ID": 568, "Code": "06000001", "Name": "新父件"}],
            123,
            [{"ID": 123, "Code": "FG-001", "Version": "V1"}],
        ])
        status = bom_write_worker.process_submission(submission, client=client)
        self.assertEqual("success", status)

    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_tplus_business_rejection_is_definite_failure(self, finish, _event):
        submission = self._submission()
        submission["request_json"] = {
            "bom": submission["request_json"],
            "custom_inventories": [{
                "kind": "parent", "code": "06000013",
                "payload": {"dto": {"Code": "06000013", "Name": "新父件"}},
            }],
        }
        rejection = ChanjetAPIError(
            "T+ 返回错误：存货编号：06000013不唯一，请尝试修改该编号中的流水号后再操作",
            endpoint="/inv/Create", status_code=500,
            business_message="存货编号：06000013不唯一，请尝试修改该编号中的流水号后再操作",
        )
        client = FakeClient([[], [], rejection])
        status = bom_write_worker.process_submission(submission, client=client)
        self.assertEqual("failed", status)
        self.assertIn("不唯一", finish.call_args.kwargs["error"]["message"])

    @patch.object(bom_write_worker, "add_event")
    @patch.object(bom_write_worker, "finish_submission")
    def test_bom_create_business_rejection_is_definite_failure(self, finish, _event):
        rejection = ChanjetAPIError(
            "T+ 返回错误：BOM 数据不合法", endpoint="/bom/Create",
            status_code=500, business_message="BOM 数据不合法",
        )
        client = FakeClient([[], rejection])
        status = bom_write_worker.process_submission(self._submission(), client=client)
        self.assertEqual("failed", status)
        self.assertIn("BOM 数据不合法", finish.call_args.kwargs["error"]["message"])

    def test_disabled_worker_never_claims(self):
        with patch.dict(os.environ, {"TPLUS_BOM_WRITE_ENABLED": "false"}, clear=False):
            claimed = []
            result = bom_write_worker.run_forever(claim=lambda: claimed.append(True), sleep=lambda _: None, max_polls=1)
        self.assertEqual(0, result)
        self.assertEqual([], claimed)


if __name__ == "__main__":
    unittest.main()
