"""决策按钮在真实 PostgreSQL 上的端到端流转（迁移 0064 的 SQL 与幂等语义）。

仅在设置 NOTIFY_DECISION_INTEGRATION_DATABASE_URL 时运行；库须已应用全部迁移（CI 的
"Apply all migrations" 步骤）。每个用例使用独立的 source_key，不清理也不依赖其他数据。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - 默认本地运行在使用前即跳过
    psycopg = None  # type: ignore[assignment]

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.notify import dispatch  # noqa: E402
from app.routers import notify as notify_router  # noqa: E402

DATABASE_URL = os.getenv("NOTIFY_DECISION_INTEGRATION_DATABASE_URL", "").strip()


@unittest.skipUnless(DATABASE_URL and psycopg is not None, "NOTIFY_DECISION_INTEGRATION_DATABASE_URL not set")
class DecisionPostgresIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = f"it-decision-{uuid.uuid4().hex[:12]}"
        self.token = uuid.uuid4().hex
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "INSERT INTO notify_sources (source_key, token_sha256, enabled, note) VALUES (%s, %s, true, %s)",
                (self.source, hashlib.sha256(self.token.encode()).hexdigest(), "decision integration test"),
            )
        app = FastAPI()
        app.include_router(notify_router.router)
        self.client = TestClient(app)
        self.headers = {"X-Notify-Source": self.source, "X-Notify-Token": self.token}
        connect = lambda: psycopg.connect(DATABASE_URL)  # noqa: E731
        self._patches = [
            mock.patch.object(notify_router, "_conn", side_effect=connect),
            mock.patch.object(dispatch, "_conn", side_effect=connect),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()

    def _send(self, dedup_key: str, question: str = "日界换装后 Redis 外发是否保留？") -> tuple[dict, dict[str, str]]:
        body = {
            "source": self.source,
            "event": "stage_brief",
            "level": "warn",
            "title": "决策集成测试",
            "summary": "正文",
            "dedup_key": dedup_key,
            "decision": {
                "key": "it:redis-swap",
                "question": question,
                "options": [
                    {"key": "keep", "label": "保留", "style": "primary"},
                    {"key": "rollback", "label": "回退", "style": "danger"},
                ],
            },
        }
        resp = self.client.post("/v1/internal/notify/send", json=body, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        # 没有匹配路由：不实际投递，按钮链接从落库的 outbox 载荷读出
        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute(
                "SELECT payload_json FROM notify_outbox WHERE id = %s", (data["outbox_id"],)
            ).fetchone()
        urls = {b["text"]: b["url"] for b in row[0]["buttons"]}
        return data, urls

    @staticmethod
    def _token(url: str) -> str:
        match = re.search(r"[?&]t=([^&]+)", url)
        assert match is not None, url
        return match.group(1)

    def test_resend_keeps_old_card_links_valid_and_choice_history(self) -> None:
        first, urls1 = self._send(f"{self.source}:msg-1")
        decision_id = first["decision"]["id"]
        self.assertEqual("pending", first["decision"]["status"])
        token1 = self._token(urls1["保留"])
        self.assertIn(f"/api/v1/notify/decisions/{decision_id}?t=", urls1["保留"])

        page = self.client.get(f"/v1/notify/decisions/{decision_id}", params={"t": token1, "opt": "keep"})
        self.assertEqual(200, page.status_code)
        self.assertIn("确认选择：保留", page.text)
        detail = self.client.get("/v1/internal/notify/decisions/by-key", params={"key": "it:redis-swap"}, headers=self.headers).json()
        self.assertEqual("pending", detail["status"])  # GET 不改状态

        posted = self.client.post(f"/v1/notify/decisions/{decision_id}", data={"t": token1, "opt": "keep"})
        self.assertEqual(200, posted.status_code)

        # 同一决策再发一张卡（不同 dedup_key）：决策不变、选择不清空、问题不被改写
        second, urls2 = self._send(f"{self.source}:msg-2", question="被改写的问题")
        self.assertEqual(decision_id, second["decision"]["id"])
        self.assertEqual("answered", second["decision"]["status"])
        token2 = self._token(urls2["回退"])
        self.assertNotEqual(token1, token2)

        # 旧卡与新卡的链接都有效
        for token in (token1, token2):
            self.assertEqual(200, self.client.get(f"/v1/notify/decisions/{decision_id}", params={"t": token}).status_code)
        self.assertEqual(200, self.client.post(f"/v1/notify/decisions/{decision_id}", data={"t": token2, "opt": "rollback"}).status_code)
        self.assertEqual(404, self.client.get(f"/v1/notify/decisions/{decision_id}", params={"t": "wrong"}).status_code)

        detail = self.client.get("/v1/internal/notify/decisions/by-key", params={"key": "it:redis-swap"}, headers=self.headers).json()
        self.assertEqual("answered", detail["status"])
        self.assertEqual("rollback", detail["chosen_option"])
        self.assertEqual("日界换装后 Redis 外发是否保留？", detail["question"])
        self.assertEqual(["keep", "rollback"], [h["option"] for h in detail["history"]])
        self.assertTrue(detail["chosen_at"].endswith("+00:00"))

        listed = self.client.get("/v1/internal/notify/decisions", params={"status": "answered"}, headers=self.headers).json()
        self.assertEqual([decision_id], [item["id"] for item in listed])
        self.assertEqual([], self.client.get("/v1/internal/notify/decisions", params={"status": "pending"}, headers=self.headers).json())


if __name__ == "__main__":
    unittest.main()
