from __future__ import annotations

import asyncio
import base64
import importlib
import struct
import sys

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from Crypto.Cipher import AES
from fastapi import BackgroundTasks, HTTPException


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "services" / "backend-api"
sys.path.insert(0, str(BACKEND_ROOT))


def _module():
    return importlib.import_module("app.integrations.wecom_kf")


def _config(mod, *, processor_url: str = ""):
    key = base64.b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    return mod.WeComKfConfig(
        corp_id="ww-test-corp",
        app_secret="app-secret",
        callback_token="callback-token",
        callback_aes_key=key,
        processor_url=processor_url,
        processor_timeout_seconds=3,
    )


def _encrypt(mod, config, message: str, *, timestamp: str = "1700000000", nonce: str = "n-1"):
    key = base64.b64decode(config.callback_aes_key + "=")
    raw = (
        b"0123456789abcdef"
        + struct.pack(">I", len(message.encode("utf-8")))
        + message.encode("utf-8")
        + config.corp_id.encode("utf-8")
    )
    pad_len = 32 - len(raw) % 32
    padded = raw + bytes([pad_len]) * pad_len
    encrypted = base64.b64encode(
        AES.new(key, AES.MODE_CBC, iv=key[:16]).encrypt(padded)
    ).decode("ascii")
    crypto = mod.crypto_for_config(config)
    signature = crypto.signature(timestamp, nonce, encrypted)
    body = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>".encode()
    return encrypted, signature, body


def _notification_xml() -> str:
    return """<xml>
<ToUserName><![CDATA[ww-test-corp]]></ToUserName>
<MsgType><![CDATA[event]]></MsgType>
<Event><![CDATA[kf_msg_or_event]]></Event>
<Token><![CDATA[sync-token]]></Token>
<OpenKfId><![CDATA[wk-test]]></OpenKfId>
</xml>"""


def test_config_requires_named_values_without_echoing_secrets(monkeypatch) -> None:
    mod = _module()
    for name in (
        "WECOM_KF_CORP_ID",
        "WECOM_KF_APP_SECRET",
        "WECOM_KF_CALLBACK_TOKEN",
        "WECOM_KF_CALLBACK_AES_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(mod.WeComKfError) as exc_info:
        mod.WeComKfConfig.from_env()

    assert "WECOM_KF_APP_SECRET" in str(exc_info.value)
    assert "app-secret" not in str(exc_info.value)


def test_url_echo_and_callback_are_verified_and_decrypted() -> None:
    mod = _module()
    config = _config(mod)
    encrypted, signature, body = _encrypt(mod, config, _notification_xml())
    crypto = mod.crypto_for_config(config)

    assert crypto.decrypt_url_echo(signature, "1700000000", "n-1", encrypted) == _notification_xml()
    plain = crypto.decrypt_callback(signature, "1700000000", "n-1", body)
    assert mod.parse_kf_notification(plain) == mod.KfNotification("sync-token", "wk-test")

    with pytest.raises(mod.WeComKfError, match="签名"):
        crypto.decrypt_callback("forged", "1700000000", "n-1", body)


def test_xml_parser_rejects_doctype() -> None:
    mod = _module()
    with pytest.raises(mod.WeComKfError, match="禁用声明"):
        mod.parse_kf_notification("<!DOCTYPE xml><xml />")


def test_access_token_is_cached_and_invalid_token_refreshes_once(monkeypatch) -> None:
    mod = _module()
    calls: list[tuple[str, dict | None]] = []
    issued = 0

    def fake_http(method, url, payload, timeout, *, attempts=2):
        nonlocal issued
        calls.append((url, payload))
        if "/gettoken?" in url:
            issued += 1
            return {"errcode": 0, "access_token": f"token-{issued}", "expires_in": 7200}
        token = parse_qs(urlparse(url).query)["access_token"][0]
        if token == "token-1":
            return {"errcode": 42001, "errmsg": "expired"}
        return {"errcode": 0, "has_more": 0, "next_cursor": "c2", "msg_list": []}

    monkeypatch.setattr(mod, "_http_json", fake_http)
    client = mod.WeComKfClient("corp", "secret")

    messages, cursor = client.sync_messages("wk", "sync")

    assert messages == []
    assert cursor == "c2"
    assert issued == 2
    assert client.access_token() == "token-2"


def test_sync_msg_follows_has_more_even_when_page_is_empty(monkeypatch) -> None:
    mod = _module()
    sync_payloads: list[dict] = []

    def fake_http(method, url, payload, timeout, *, attempts=2):
        if "/gettoken?" in url:
            return {"errcode": 0, "access_token": "token", "expires_in": 7200}
        sync_payloads.append(payload)
        if len(sync_payloads) == 1:
            return {"errcode": 0, "has_more": 1, "next_cursor": "c1", "msg_list": []}
        return {
            "errcode": 0,
            "has_more": 0,
            "next_cursor": "c2",
            "msg_list": [{"msgid": "m1", "origin": 3, "msgtype": "text"}],
        }

    monkeypatch.setattr(mod, "_http_json", fake_http)
    messages, cursor = mod.WeComKfClient("corp", "secret").sync_messages("wk", "sync")

    assert [item["msgid"] for item in messages] == ["m1"]
    assert cursor == "c2"
    assert sync_payloads[1]["cursor"] == "c1"


def test_send_text_uses_deterministic_reply_id_and_utf8_limit(monkeypatch) -> None:
    mod = _module()
    sent: list[dict] = []

    def fake_http(method, url, payload, timeout, *, attempts=2):
        if "/gettoken?" in url:
            return {"errcode": 0, "access_token": "token", "expires_in": 7200}
        sent.append(payload)
        return {"errcode": 0, "errmsg": "ok", "msgid": payload["msgid"]}

    monkeypatch.setattr(mod, "_http_json", fake_http)
    client = mod.WeComKfClient("corp", "secret")
    client.send_text("wm-user", "wk", "你" * 1000, "source-1")
    client.send_text("wm-user", "wk", "second", "source-1")

    assert sent[0]["msgid"] == sent[1]["msgid"] == mod.reply_msgid("source-1")
    assert len(sent[0]["text"]["content"].encode("utf-8")) <= 2048


def test_notification_only_replies_to_external_customer_text() -> None:
    mod = _module()
    config = _config(mod)

    class FakeClient:
        def __init__(self):
            self.sent = []

        def sync_messages(self, open_kfid, sync_token, cursor=""):
            return [
                {"msgid": "m1", "open_kfid": open_kfid, "external_userid": "wm1", "origin": 3,
                 "msgtype": "text", "text": {"content": "测试123"}},
                {"msgid": "m2", "open_kfid": open_kfid, "external_userid": "wm1", "origin": 5,
                 "msgtype": "text", "text": {"content": "servicer"}},
                {"msgid": "m3", "open_kfid": open_kfid, "external_userid": "wm1", "origin": 3,
                 "msgtype": "image", "image": {"media_id": "media"}},
            ], "cursor-next"

        def send_text(self, external_userid, open_kfid, content, source_msgid):
            self.sent.append((external_userid, open_kfid, content, source_msgid))

    fake = FakeClient()
    mod._cursors.clear()
    mod.handle_notification(mod.KfNotification("sync", "wk"), config, client=fake)

    assert fake.sent == [("wm1", "wk", "已收到：测试123", "m1")]
    assert mod._cursors["wk"] == "cursor-next"


def test_processor_forwards_openai_compatible_payload(monkeypatch) -> None:
    mod = _module()
    captured = {}

    def fake_http(method, url, payload, timeout, *, attempts=2):
        captured.update({"url": url, "payload": payload, "attempts": attempts})
        return {"choices": [{"message": {"content": "处理结果"}}]}

    monkeypatch.setattr(mod, "_http_json", fake_http)
    message = {
        "msgid": "m1",
        "open_kfid": "wk1",
        "external_userid": "wm1",
        "text": {"content": "请处理"},
    }
    reply = mod.build_reply(message, _config(mod, processor_url="http://bridge/v1/chat/completions"))

    assert reply == "处理结果"
    assert captured["attempts"] == 1
    assert captured["payload"]["metadata"]["channel"] == "wecom"
    assert captured["payload"]["metadata"]["transport"] == "wecom_kf"
    assert captured["payload"]["metadata"]["account_id"] == "kf:wk1"
    assert captured["payload"]["metadata"]["peer_id"] == "wm1"
    assert captured["payload"]["metadata"]["conversation_id"] == "wm1"
    assert captured["payload"]["metadata"]["chatgpt_project"] == "WeCom-KF"


def test_callback_route_validates_before_scheduling(monkeypatch) -> None:
    mod = _module()
    router_mod = importlib.import_module("app.routers.webhooks.wecom")
    config = _config(mod)
    _, signature, body = _encrypt(mod, config, _notification_xml())

    class FakeRequest:
        async def body(self):
            return body

    monkeypatch.setattr(router_mod.WeComKfConfig, "from_env", classmethod(lambda cls: config))
    tasks = BackgroundTasks()
    response = asyncio.run(
        router_mod.receive_wecom_kf_callback(
            FakeRequest(), tasks, signature, "1700000000", "n-1"
        )
    )

    assert response.body == b"success"
    assert len(tasks.tasks) == 1

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            router_mod.receive_wecom_kf_callback(
                FakeRequest(), BackgroundTasks(), "forged", "1700000000", "n-1"
            )
        )
    assert exc_info.value.status_code == 400
