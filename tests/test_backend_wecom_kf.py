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


def test_send_menu_has_static_confirmation_actions(monkeypatch) -> None:
    mod = _module()
    sent: list[dict] = []

    def fake_http(method, url, payload, timeout, *, attempts=2):
        if "/gettoken?" in url:
            return {"errcode": 0, "access_token": "token", "expires_in": 7200}
        sent.append(payload)
        return {"errcode": 0, "errmsg": "ok"}

    monkeypatch.setattr(mod, "_http_json", fake_http)
    task_key = "a" * 32
    mod.WeComKfClient("corp", "secret").send_menu(
        "wm-user", "wk", "分析结果", "source-1", task_key
    )

    payload = sent[0]
    assert payload["msgtype"] == "msgmenu"
    assert [item["click"]["content"] for item in payload["msgmenu"]["list"]] == [
        "确认处理", "补充资料", "取消任务"
    ]
    assert payload["msgmenu"]["list"][0]["click"]["id"] == f"kf_confirm_{task_key}"


def test_download_media_returns_binary_and_filename(monkeypatch) -> None:
    mod = _module()

    class Response:
        headers = {
            "Content-Type": "image/jpeg",
            "Content-Disposition": "attachment; filename*=UTF-8''%E5%90%88%E5%90%8C.jpg",
        }

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return b"jpeg-data"

    client = mod.WeComKfClient("corp", "secret")
    monkeypatch.setattr(client, "access_token", lambda **_: "token")
    monkeypatch.setattr(mod.request, "urlopen", lambda req, timeout: Response())

    media = client.download_media("media-1")

    assert media.data == b"jpeg-data"
    assert media.mime_type == "image/jpeg"
    assert media.filename == "合同.jpg"


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


def test_notification_persists_task_cursor_and_outbound_status() -> None:
    mod = _module()
    config = _config(mod)

    class FakeStore:
        def __init__(self):
            self.cursor = "cursor-old"
            self.task = None
            self.outbound = []
            self.failed = []

        def get_cursor(self, open_kfid):
            return self.cursor

        def set_cursor(self, open_kfid, cursor):
            self.cursor = cursor

        def active_task(self, open_kfid, external_userid):
            return self.task

        def create_task(self, open_kfid, external_userid, title):
            self.task = {
                "id": 1, "task_key": "b" * 32, "status": "collecting", "title": title
            }
            return self.task

        def record_outbound(self, msgid, task_id, open_kfid, external_userid, purpose):
            self.outbound.append([msgid, task_id, purpose, "queued"])

        def mark_outbound_sent(self, msgid):
            self.outbound[-1][-1] = "sent"

        def mark_outbound_failed(self, msgid, fail_type):
            self.failed.append((msgid, fail_type))

    class FakeClient:
        def __init__(self):
            self.sent = []

        def sync_messages(self, open_kfid, sync_token, cursor=""):
            assert cursor == "cursor-old"
            return [
                {
                    "msgid": "m1", "open_kfid": open_kfid, "external_userid": "wm1",
                    "origin": 3, "msgtype": "text", "text": {"content": "开始任务：合同"},
                },
                {
                    "msgid": "event-1", "msgtype": "event",
                    "event": {"event_type": "msg_send_fail", "fail_msgid": "old-reply", "fail_type": 2},
                },
            ], "cursor-next"

        def download_media(self, media_id):
            raise AssertionError("not expected")

        def send_text(self, external_userid, open_kfid, content, source_msgid, *, purpose="reply"):
            self.sent.append((content, purpose))

    store = FakeStore()
    client = FakeClient()
    mod.handle_notification(
        mod.KfNotification("sync", "wk"), config, client=client, task_store=store
    )

    assert store.cursor == "cursor-next"
    assert client.sent[0][1] == "task_started"
    assert store.outbound[0][2:] == ["task_started", "sent"]
    assert store.failed == [("old-reply", 2)]


def test_send_text_treats_repeated_msgid_as_success(monkeypatch) -> None:
    mod = _module()

    def fake_http(method, url, payload, timeout, *, attempts=2):
        if "/gettoken?" in url:
            return {"errcode": 0, "access_token": "token", "expires_in": 7200}
        return {"errcode": 95033, "errmsg": "repeated msgid"}

    monkeypatch.setattr(mod, "_http_json", fake_http)
    # 95033 表示该回复此前已成功下发，属幂等成功，send_text 不应抛异常。
    result = mod.WeComKfClient("corp", "secret").send_text("wm", "wk", "hi", "m1")
    assert int(result["errcode"]) == 95033


def test_one_failed_message_does_not_block_later_messages() -> None:
    mod = _module()
    config = _config(mod)

    class FakeStore:
        def __init__(self):
            self.cursor = "old"

        def get_cursor(self, open_kfid):
            return self.cursor

        def set_cursor(self, open_kfid, cursor):
            self.cursor = cursor

        def active_task(self, open_kfid, external_userid):
            return None

        def outbound_already_sent(self, msgid):
            return False

        def record_outbound(self, *args):
            pass

        def mark_outbound_sent(self, msgid):
            pass

        def mark_outbound_failed(self, *args):
            pass

    class FakeClient:
        def __init__(self):
            self.sent = []

        def sync_messages(self, open_kfid, sync_token, cursor=""):
            return [
                {"msgid": "m1", "open_kfid": open_kfid, "external_userid": "wm1",
                 "origin": 3, "msgtype": "text", "text": {"content": "旧消息"}},
                {"msgid": "m2", "open_kfid": open_kfid, "external_userid": "wm1",
                 "origin": 3, "msgtype": "text", "text": {"content": "新消息"}},
            ], "next"

        def download_media(self, media_id):
            raise AssertionError("not expected")

        def send_text(self, external_userid, open_kfid, content, source_msgid, *, purpose="reply"):
            if source_msgid == "m1":
                raise mod.WeComKfError("boom")
            self.sent.append(source_msgid)

    store = FakeStore()
    client = FakeClient()
    mod.handle_notification(
        mod.KfNotification("sync", "wk"), config, client=client, task_store=store
    )

    # 第一条硬失败被隔离，第二条仍处理，游标照常推进（否则整批永久卡死）。
    assert client.sent == ["m2"]
    assert store.cursor == "next"


def test_text_reply_skipped_when_already_sent() -> None:
    mod = _module()
    config = _config(mod)

    class FakeStore:
        def __init__(self):
            self.cursor = "old"

        def get_cursor(self, open_kfid):
            return self.cursor

        def set_cursor(self, open_kfid, cursor):
            self.cursor = cursor

        def active_task(self, open_kfid, external_userid):
            return None

        def outbound_already_sent(self, msgid):
            return True

        def record_outbound(self, *args):
            raise AssertionError("must not record an already-sent reply")

        def mark_outbound_sent(self, msgid):
            raise AssertionError("must not resend")

        def mark_outbound_failed(self, *args):
            pass

    class FakeClient:
        def __init__(self):
            self.sent = []

        def sync_messages(self, open_kfid, sync_token, cursor=""):
            return [
                {"msgid": "m1", "open_kfid": open_kfid, "external_userid": "wm1",
                 "origin": 3, "msgtype": "text", "text": {"content": "旧消息"}},
            ], "next"

        def download_media(self, media_id):
            raise AssertionError("not expected")

        def send_text(self, *args, **kwargs):
            self.sent.append(args)

    store = FakeStore()
    client = FakeClient()
    mod.handle_notification(
        mod.KfNotification("sync", "wk"), config, client=client, task_store=store
    )

    # 回放 backlog 时，已成功回复过的旧文本必须跳过，不重复调用处理器、不重复回复。
    assert client.sent == []
    assert store.cursor == "next"


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


def test_processor_forwards_material_attachments_as_data_urls(monkeypatch) -> None:
    mod = _module()
    task_mod = importlib.import_module("app.integrations.wecom_kf_tasks")
    captured = {}

    def fake_http(method, url, payload, timeout, *, attempts=2):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "分析结果"}}]}

    monkeypatch.setattr(mod, "_http_json", fake_http)
    attachment = task_mod.ProcessorAttachment(
        "合同.pdf", "application/pdf", 3, "data:application/pdf;base64,cGRm"
    )
    reply = mod.call_processor(
        {"msgid": "m1", "open_kfid": "wk1", "external_userid": "wm1"},
        _config(mod, processor_url="http://bridge/v1/chat/completions"),
        "分析这些资料",
        [attachment],
    )

    content = captured["payload"]["messages"][0]["content"]
    assert reply == "分析结果"
    assert content[0] == {"type": "text", "text": "分析这些资料"}
    assert content[1]["image_url"]["url"] == attachment.data_url


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


def test_sync_msg_omits_token_when_empty(monkeypatch) -> None:
    mod = _module()
    sync_payloads: list[dict] = []

    def fake_http(method, url, payload, timeout, *, attempts=2):
        if "/gettoken?" in url:
            return {"errcode": 0, "access_token": "token", "expires_in": 7200}
        sync_payloads.append(payload)
        return {"errcode": 0, "has_more": 0, "next_cursor": "c1", "msg_list": []}

    monkeypatch.setattr(mod, "_http_json", fake_http)
    client = mod.WeComKfClient("corp", "secret")

    # 主动轮询没有事件 Token：只带游标，不带 token 字段。
    client.sync_messages("wk", "", "c0")
    assert "token" not in sync_payloads[0]
    assert sync_payloads[0]["cursor"] == "c0"

    # 回调路径仍带事件 Token。
    client.sync_messages("wk", "event-token", "c0")
    assert sync_payloads[1]["token"] == "event-token"


def test_list_account_ids_filters_malformed_entries(monkeypatch) -> None:
    mod = _module()

    def fake_http(method, url, payload, timeout, *, attempts=2):
        if "/gettoken?" in url:
            return {"errcode": 0, "access_token": "token", "expires_in": 7200}
        return {
            "errcode": 0,
            "account_list": [
                {"open_kfid": "wk-a", "name": "A"},
                {"open_kfid": "  "},
                "junk",
                {"open_kfid": "wk-b"},
            ],
        }

    monkeypatch.setattr(mod, "_http_json", fake_http)
    assert mod.WeComKfClient("corp", "secret").list_account_ids() == ["wk-a", "wk-b"]


def test_poll_accounts_once_syncs_each_account_without_sync_token() -> None:
    mod = _module()
    config = _config(mod)

    class FakeClient:
        def __init__(self):
            self.synced = []
            self.sent = []

        def list_account_ids(self):
            return ["wk-a", "wk-b"]

        def sync_messages(self, open_kfid, sync_token, cursor=""):
            # 轮询路径没有事件 Token。
            assert sync_token == ""
            self.synced.append((open_kfid, cursor))
            if open_kfid == "wk-a":
                return [
                    {"msgid": "m1", "open_kfid": open_kfid, "external_userid": "wm1",
                     "origin": 3, "msgtype": "text", "text": {"content": "漏投递的消息"}},
                ], "next-a"
            return [], "next-b"

        def send_text(self, external_userid, open_kfid, content, source_msgid):
            self.sent.append((external_userid, open_kfid, source_msgid))

    fake = FakeClient()
    mod._cursors.clear()
    mod._cursors.update({"wk-a": "cur-a", "wk-b": "cur-b"})

    polled = mod.poll_accounts_once(config, client=fake)

    assert polled == 2
    assert fake.synced == [("wk-a", "cur-a"), ("wk-b", "cur-b")]
    assert fake.sent == [("wm1", "wk-a", "m1")]
    assert mod._cursors == {"wk-a": "next-a", "wk-b": "next-b"}


def test_poller_startup_disabled_by_zero_interval_or_missing_config(monkeypatch) -> None:
    router_mod = importlib.import_module("app.routers.webhooks.wecom")
    started: list[str] = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started.append(kwargs.get("name", ""))

        def start(self):
            pass

    monkeypatch.setattr(router_mod.threading, "Thread", FakeThread)

    monkeypatch.setenv("WECOM_KF_POLL_INTERVAL_SECONDS", "0")
    router_mod._start_kf_poller()
    assert started == []

    # 间隔有效但缺少 kf 配置时同样不启动。
    monkeypatch.setenv("WECOM_KF_POLL_INTERVAL_SECONDS", "300")
    for name in (
        "WECOM_KF_CORP_ID",
        "WECOM_KF_APP_SECRET",
        "WECOM_KF_CALLBACK_TOKEN",
        "WECOM_KF_CALLBACK_AES_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    router_mod._start_kf_poller()
    assert started == []


def test_poller_startup_is_idempotent(monkeypatch) -> None:
    mod = _module()
    router_mod = importlib.import_module("app.routers.webhooks.wecom")
    started: list[str] = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started.append(kwargs.get("name", ""))

        def start(self):
            pass

    monkeypatch.setattr(router_mod.threading, "Thread", FakeThread)
    monkeypatch.setattr(router_mod, "_poller_started", False)
    monkeypatch.setenv("WECOM_KF_POLL_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("WECOM_KF_CORP_ID", "corp")
    monkeypatch.setenv("WECOM_KF_APP_SECRET", "secret")
    monkeypatch.setenv("WECOM_KF_CALLBACK_TOKEN", "token")
    monkeypatch.setenv(
        "WECOM_KF_CALLBACK_AES_KEY",
        _config(mod).callback_aes_key,
    )

    # startup 被重复触发时（2026-07-23 生产观察到 3 次），只允许一个轮询线程。
    router_mod._start_kf_poller()
    router_mod._start_kf_poller()
    router_mod._start_kf_poller()
    assert started == ["wecom-kf-poller"]
