"""Postgres integration. Uses an explicitly selected disposable database only."""
import base64
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg


@unittest.skipUnless(os.getenv("NOTIFY_WECHAT_TEST_DSN"), "requires disposable Postgres")
class PersonalWechatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_path = sys.path[:]
        cls.old_app = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
        for key in cls.old_app:
            del sys.modules[key]
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "services/backend-api"))
        from app.notify import personal_wechat, store
        cls.wechat, cls.store = personal_wechat, store
        cls.db = psycopg.connect(os.environ["NOTIFY_WECHAT_TEST_DSN"])
        ident = "test_wechat_" + os.urandom(6).hex()
        cls.db.execute("CREATE SCHEMA " + ident)
        cls.db.execute("SET search_path TO " + ident)
        for name in ("0052_notify_center.sql", "0066_notify_personal_wechat.sql"):
            cls.db.execute((root / "db/migrations" / name).read_text())
        cls.db.commit()
        cls.schema = ident

    @classmethod
    def tearDownClass(cls):
        cls.db.rollback()
        cls.db.execute("DROP SCHEMA " + cls.schema + " CASCADE")
        cls.db.commit()
        cls.db.close()
        for key in list(sys.modules):
            if key == "app" or key.startswith("app."):
                del sys.modules[key]
        sys.modules.update(cls.old_app)
        sys.path[:] = cls.old_path

    def setUp(self):
        self.db.execute("TRUNCATE notify_outbox,notify_deliveries,notify_wechat_payloads,notify_routes,notify_sources")
        self.db.execute("INSERT INTO notify_sources(source_key,token_sha256) VALUES ('mail-hub','unused')")
        self.db.execute("""
            INSERT INTO notify_routes(source_key,event_pattern,channel,target_json)
            VALUES ('mail-hub','haier.otp.received','personal_wechat',
                    '{"alias":"test-group","agent":"test-agent","username":"fake@chatroom","sensitive":true}')
        """)
        self.db.commit()
        patcher = patch.dict(os.environ, {"NOTIFY_WECHAT_PAYLOAD_KEY": base64.b64encode(b"x"*32).decode()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.db.rollback)

    def submit(self, content=None, **overrides):
        data = {
            "event":"haier.otp.received", "dedup_key":"sample",
            "target":"test-group","sensitive":True,
            "expires_at":datetime.now(timezone.utc).replace(microsecond=0)+timedelta(minutes=4),
            "content":content or [{"type":"text","text":"synthetic 999000"}],
        } | overrides
        body=self.wechat.Submission.model_validate(data)
        result=self.wechat.submit(self.db,"mail-hub",body)
        self.db.commit()
        return body,result

    def test_sealed_delivery_and_idempotent_receipt(self):
        body, queued = self.submit()
        duplicate = self.wechat.submit(self.db,"mail-hub",body)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(queued["delivery_id"],duplicate["delivery_id"])
        self.assertNotIn("999000",str(self.db.execute("SELECT payload_json FROM notify_outbox").fetchone()))
        self.assertNotIn(b"999000",bytes(self.db.execute("SELECT ciphertext FROM notify_wechat_payloads").fetchone()[0]))
        self.assertIsNone(self.wechat.claim(self.db, "other-agent"))
        task = self.wechat.claim(self.db, "test-agent")
        self.assertNotIn("content",task)
        self.assertEqual(self.wechat.begin(self.db, "test-agent",task["delivery_id"],task["lease"])["step"],0)
        receipt=self.wechat.Receipt(lease=task["lease"],step=0,outcome="sent",message_id="123")
        for _ in range(2):
            self.assertEqual(self.wechat.finish(self.db,"test-agent",task["delivery_id"],receipt),{"status":"sent"})
        self.assertIsNone(self.db.execute("SELECT ciphertext FROM notify_wechat_payloads").fetchone()[0])
        proof=self.store.delivery_receipt(self.db,queued["outbox_id"],"mail-hub")
        self.assertEqual(proof["deliveries"][0]["parts"]["0"]["message_id"],"123")

    def test_route_disable_blocks_claimed_delivery(self):
        self.submit()
        task=self.wechat.claim(self.db,"test-agent")
        self.db.execute("UPDATE notify_routes SET enabled=false")
        with self.assertRaisesRegex(Exception,"delivery lease or route unavailable"):
            self.wechat.begin(self.db,"test-agent",task["delivery_id"],task["lease"])
        self.wechat.cleanup(self.db)
        self.assertEqual(self.db.execute("SELECT status FROM notify_deliveries").fetchone()[0],"cancelled")
        self.assertIsNone(self.db.execute("SELECT ciphertext FROM notify_wechat_payloads").fetchone()[0])

    def test_expired_send_is_unknown_and_not_reclaimed(self):
        self.submit()
        task=self.wechat.claim(self.db,"test-agent")
        self.wechat.begin(self.db,"test-agent",task["delivery_id"],task["lease"])
        self.db.execute("UPDATE notify_wechat_payloads SET lease_until=now()-interval '1 minute'")
        self.assertIsNone(self.wechat.claim(self.db,"test-agent"))
        self.assertEqual(self.db.execute("SELECT status FROM notify_deliveries").fetchone()[0],"unknown")

    def test_ordered_text_and_image_keep_separate_receipts(self):
        png=base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic").decode()
        _, queued=self.submit([{"type":"text","text":"one"},
                                {"type":"image","ref":"part-two","png_base64":png}], sensitive=False)
        for index, kind in enumerate(("text","image")):
            task=self.wechat.claim(self.db,"test-agent")
            step=self.wechat.begin(self.db,"test-agent",task["delivery_id"],task["lease"])
            self.assertEqual(step["content"]["type"],kind)
            self.assertEqual(step["step"],index)
            self.wechat.finish(self.db,"test-agent",task["delivery_id"],
                self.wechat.Receipt(lease=task["lease"],step=index,outcome="sent",message_id=str(index+1)))
        self.assertIsNone(self.wechat.claim(self.db,"test-agent"))
        proof=self.store.delivery_receipt(self.db,queued["outbox_id"],"mail-hub")
        self.assertEqual(len(proof["deliveries"][0]["parts"]),2)
