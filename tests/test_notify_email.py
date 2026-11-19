from __future__ import annotations

import os
import sys
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "services" / "backend-api"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.notify.channels import email, sender_for
from app.notify.models import Notification, NotifyChannelPayload


class FakeSMTP:
    def __init__(self, host: str, port: int, timeout: float = 20.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ehlo_called = 0
        self.starttls_called = False
        self.logged_in: tuple[str, str] | None = None
        self.sent_messages: list[tuple[Any, str, list[str]]] = []
        self.quit_called = False

    def ehlo(self) -> None:
        self.ehlo_called += 1

    def has_extn(self, extn: str) -> bool:
        return extn.lower() == "starttls"

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> None:
        self.sent_messages.append((msg, from_addr, to_addrs))

    def quit(self) -> None:
        self.quit_called = True


class EmailChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_sender_for_email_registered(self) -> None:
        sender = sender_for("email")
        self.assertEqual(sender, email.send)

    def test_credentials_fallback(self) -> None:
        os.environ["SMTP_USER"] = "test@example.com"
        os.environ["SMTP_PASS"] = "secret123"
        creds = email.credentials("default")
        self.assertEqual(creds["host"], "smtp.qq.com")
        self.assertEqual(creds["port"], 587)
        self.assertEqual(creds["user"], "test@example.com")
        self.assertEqual(creds["password"], "secret123")
        self.assertEqual(creds["from_addr"], "test@example.com")

        # 兼容旧 EMAIL_ADDRESS_QQ / EMAIL_PASSWORD_QQ
        os.environ.pop("SMTP_USER")
        os.environ.pop("SMTP_PASS")
        os.environ["EMAIL_ADDRESS_QQ"] = "qq_user@qq.com"
        os.environ["EMAIL_PASSWORD_QQ"] = "qq_pass"
        creds_qq = email.credentials("default")
        self.assertEqual(creds_qq["user"], "qq_user@qq.com")
        self.assertEqual(creds_qq["password"], "qq_pass")

    def test_send_with_custom_html_payload(self) -> None:
        os.environ["SMTP_USER"] = "system@test.com"
        os.environ["SMTP_PASS"] = "authcode"

        table_html = "<table border='1'><tr><th>仓库</th><th>存货编码</th><th>主数量</th></tr><tr><td>成品库</td><td>10001</td><td>100</td></tr></table>"
        notification = Notification(
            source="tplus-sync-worker",
            event="home_stock_push",
            title="【畅捷通存量】家里库存查询（等待您查看）",
            summary="存量数据推送",
            channel_payloads={
                "email": NotifyChannelPayload(
                    msg_type="html",
                    content={"html": table_html},
                )
            },
        )

        fake_smtp = FakeSMTP("smtp.qq.com", 587)

        email.send(
            notification,
            target={"to": ["recipient@example.com"], "cc": ["cc@example.com"]},
            smtp_client_factory=lambda *args, **kwargs: fake_smtp,
        )

        self.assertEqual(len(fake_smtp.sent_messages), 1)
        msg, from_addr, to_addrs = fake_smtp.sent_messages[0]
        self.assertEqual(from_addr, "system@test.com")
        self.assertIn("recipient@example.com", to_addrs)
        self.assertIn("cc@example.com", to_addrs)

        # 检查邮件主题和正文
        self.assertIn("等待您查看", msg["Subject"])
        # multipart
        payload_html = ""
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload_html = part.get_payload(decode=True).decode("utf-8")
        self.assertIn("成品库", payload_html)
        self.assertIn("10001", payload_html)

    def test_send_with_generic_rendering(self) -> None:
        os.environ["SMTP_USER"] = "system@test.com"
        os.environ["SMTP_PASS"] = "authcode"

        notification = Notification(
            source="sso",
            event="email_verification",
            level="info",
            title="SSO 验证码",
            summary="您的验证码为 849201，5分钟内有效。",
            segments=[
                {"kind": "fields", "fields": [{"name": "验证码", "value": "849201", "note": "请勿泄露"}]},
            ],
            buttons=[
                {"text": "前往登录", "url": "https://auth.hydwang.xyz"}
            ],
        )

        fake_smtp = FakeSMTP("smtp.qq.com", 587)
        email.send(
            notification,
            target={"to": "user@example.com"},
            smtp_client_factory=lambda *args, **kwargs: fake_smtp,
        )

        self.assertEqual(len(fake_smtp.sent_messages), 1)
        msg, from_addr, to_addrs = fake_smtp.sent_messages[0]
        self.assertEqual(to_addrs, ["user@example.com"])
        payload_html = ""
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload_html = part.get_payload(decode=True).decode("utf-8")
        self.assertIn("SSO 验证码", payload_html)
        self.assertIn("849201", payload_html)
        self.assertIn("https://auth.hydwang.xyz", payload_html)

    def test_send_missing_recipient_raises(self) -> None:
        notification = Notification(
            source="test",
            event="test",
            title="Test",
        )
        with self.assertRaises(RuntimeError) as cm:
            email.send(notification, target={"to": []})
        self.assertIn("no recipients", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
