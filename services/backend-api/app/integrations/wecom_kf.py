"""微信客服 API 的最小文本闭环。

责任仅限于：回调验签/解密、sync_msg 拉取、可选转发给
OpenAI-compatible 处理器，以及 send_msg 文本回复。媒体、持久化游标和
完整消息库留给后续阶段。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import threading
import time
import xml.etree.ElementTree as ET

from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

from Crypto.Cipher import AES


WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
TOKEN_ERROR_CODES = {40014, 42001}
MAX_CALLBACK_BYTES = 1024 * 1024
MAX_SYNC_PAGES = 100

logger = logging.getLogger("aliecs.wecom_kf")


class WeComKfError(RuntimeError):
    pass


@dataclass(frozen=True)
class WeComKfConfig:
    corp_id: str
    app_secret: str
    callback_token: str
    callback_aes_key: str
    api_timeout_seconds: float = 10.0
    processor_url: str = ""
    processor_timeout_seconds: float = 1260.0

    @classmethod
    def from_env(cls) -> "WeComKfConfig":
        values = {
            "corp_id": os.getenv("WECOM_KF_CORP_ID", "").strip(),
            "app_secret": os.getenv("WECOM_KF_APP_SECRET", "").strip(),
            "callback_token": os.getenv("WECOM_KF_CALLBACK_TOKEN", "").strip(),
            "callback_aes_key": os.getenv("WECOM_KF_CALLBACK_AES_KEY", "").strip(),
        }
        missing = [
            env_name
            for field, env_name in (
                ("corp_id", "WECOM_KF_CORP_ID"),
                ("app_secret", "WECOM_KF_APP_SECRET"),
                ("callback_token", "WECOM_KF_CALLBACK_TOKEN"),
                ("callback_aes_key", "WECOM_KF_CALLBACK_AES_KEY"),
            )
            if not values[field]
        ]
        if missing:
            raise WeComKfError("缺少微信客服配置: " + ", ".join(missing))
        return cls(
            **values,
            api_timeout_seconds=_positive_env_float("WECOM_KF_API_TIMEOUT_SECONDS", 10.0),
            processor_url=os.getenv("WECOM_KF_PROCESSOR_URL", "").strip(),
            processor_timeout_seconds=_positive_env_float(
                "WECOM_KF_PROCESSOR_TIMEOUT_SECONDS", 1260.0
            ),
        )


@dataclass(frozen=True)
class KfNotification:
    sync_token: str
    open_kfid: str


def _positive_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise WeComKfError(f"{name} 必须是数字") from exc
    if value <= 0:
        raise WeComKfError(f"{name} 必须大于 0")
    return value


def _parse_xml(value: bytes | str) -> ET.Element:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) > MAX_CALLBACK_BYTES:
        raise WeComKfError("回调 XML 超过大小限制")
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise WeComKfError("回调 XML 包含禁用声明")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise WeComKfError("回调 XML 格式错误") from exc


class WeComKfCrypto:
    """企业微信 WXBizMsgCrypt 的 AES-CBC 等价实现。"""

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str) -> None:
        if len(encoding_aes_key) != 43:
            raise WeComKfError("WECOM_KF_CALLBACK_AES_KEY 必须是 43 位 EncodingAESKey")
        try:
            key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except ValueError as exc:
            raise WeComKfError("WECOM_KF_CALLBACK_AES_KEY 格式错误") from exc
        if len(key) != 32:
            raise WeComKfError("WECOM_KF_CALLBACK_AES_KEY 解码后必须是 32 字节")
        self._token = token
        self._key = key
        self._receive_id = receive_id

    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        joined = "".join(sorted((self._token, str(timestamp), str(nonce), encrypted)))
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()

    def verify_and_decrypt(
        self, signature: str, timestamp: str, nonce: str, encrypted: str
    ) -> str:
        expected = self.signature(timestamp, nonce, encrypted)
        if not hmac.compare_digest(str(signature), expected):
            raise WeComKfError("微信客服回调签名校验失败")
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
            padded = AES.new(self._key, AES.MODE_CBC, iv=self._key[:16]).decrypt(ciphertext)
        except (ValueError, TypeError) as exc:
            raise WeComKfError("微信客服回调密文格式错误") from exc
        plain = _unpad32(padded)
        if len(plain) < 20:
            raise WeComKfError("微信客服回调明文长度错误")
        msg_len = struct.unpack(">I", plain[16:20])[0]
        end = 20 + msg_len
        if end > len(plain):
            raise WeComKfError("微信客服回调明文长度不匹配")
        message = plain[20:end]
        receive_id = plain[end:].decode("utf-8", errors="strict")
        if self._receive_id and not hmac.compare_digest(receive_id, self._receive_id):
            raise WeComKfError("微信客服回调 CorpID 不匹配")
        try:
            return message.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeComKfError("微信客服回调明文不是 UTF-8") from exc

    def decrypt_url_echo(
        self, signature: str, timestamp: str, nonce: str, encrypted_echo: str
    ) -> str:
        return self.verify_and_decrypt(signature, timestamp, nonce, encrypted_echo)

    def decrypt_callback(
        self, signature: str, timestamp: str, nonce: str, body: bytes
    ) -> str:
        root = _parse_xml(body)
        encrypted = (root.findtext("Encrypt") or "").strip()
        if not encrypted:
            raise WeComKfError("微信客服回调缺少 Encrypt")
        return self.verify_and_decrypt(signature, timestamp, nonce, encrypted)


def _unpad32(value: bytes) -> bytes:
    if not value:
        raise WeComKfError("微信客服回调填充为空")
    count = value[-1]
    if count < 1 or count > 32 or value[-count:] != bytes([count]) * count:
        raise WeComKfError("微信客服回调填充错误")
    return value[:-count]


def parse_kf_notification(plain_xml: str) -> KfNotification | None:
    root = _parse_xml(plain_xml)
    if (root.findtext("MsgType") or "").strip() != "event":
        return None
    if (root.findtext("Event") or "").strip() != "kf_msg_or_event":
        return None
    sync_token = (root.findtext("Token") or "").strip()
    open_kfid = (root.findtext("OpenKfId") or "").strip()
    if not sync_token or not open_kfid:
        raise WeComKfError("微信客服事件缺少 Token 或 OpenKfId")
    return KfNotification(sync_token=sync_token, open_kfid=open_kfid)


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    timeout: float,
    *,
    attempts: int = 2,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            result = json.loads(body) if body else {}
            if not isinstance(result, dict):
                raise WeComKfError("远程接口返回非对象 JSON")
            return result
        except error.HTTPError as exc:
            last_error = exc
            if exc.code < 500 or attempt + 1 >= attempts:
                raise WeComKfError(f"远程接口 HTTP {exc.code}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise WeComKfError("远程接口连接失败或超时") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WeComKfError("远程接口返回无效 JSON") from exc
        time.sleep(0.2)
    raise WeComKfError("远程接口请求失败") from last_error


class WeComKfClient:
    def __init__(self, corp_id: str, app_secret: str, timeout: float = 10.0) -> None:
        self._corp_id = corp_id
        self._app_secret = app_secret
        self._timeout = timeout
        self._access_token = ""
        self._token_deadline = 0.0
        self._token_lock = threading.Lock()

    def access_token(self, *, force_refresh: bool = False) -> str:
        now = time.monotonic()
        if not force_refresh and self._access_token and now < self._token_deadline:
            return self._access_token
        with self._token_lock:
            now = time.monotonic()
            if not force_refresh and self._access_token and now < self._token_deadline:
                return self._access_token
            query = parse.urlencode({"corpid": self._corp_id, "corpsecret": self._app_secret})
            result = _http_json(
                "GET", f"{WECOM_API_BASE}/gettoken?{query}", None, self._timeout
            )
            if int(result.get("errcode", -1)) != 0 or not result.get("access_token"):
                raise _api_error("gettoken", result)
            expires_in = max(1, int(result.get("expires_in", 7200)))
            self._access_token = str(result["access_token"])
            self._token_deadline = time.monotonic() + max(1, expires_in - 120)
            return self._access_token

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            token = self.access_token(force_refresh=attempt == 1)
            query = parse.urlencode({"access_token": token})
            result = _http_json(
                "POST", f"{WECOM_API_BASE}{path}?{query}", payload, self._timeout
            )
            errcode = int(result.get("errcode", -1))
            if errcode in TOKEN_ERROR_CODES and attempt == 0:
                continue
            if errcode != 0:
                raise _api_error(path, result)
            return result
        raise WeComKfError(f"{path} access_token 刷新后仍失败")

    def sync_messages(
        self, open_kfid: str, sync_token: str, cursor: str = ""
    ) -> tuple[list[dict[str, Any]], str]:
        messages: list[dict[str, Any]] = []
        current_cursor = cursor
        for _ in range(MAX_SYNC_PAGES):
            payload: dict[str, Any] = {
                "token": sync_token,
                "limit": 1000,
                "voice_format": 0,
                "open_kfid": open_kfid,
            }
            if current_cursor:
                payload["cursor"] = current_cursor
            page = self._post("/kf/sync_msg", payload)
            page_messages = page.get("msg_list") or []
            if not isinstance(page_messages, list):
                raise WeComKfError("sync_msg 的 msg_list 格式错误")
            messages.extend(item for item in page_messages if isinstance(item, dict))
            next_cursor = str(page.get("next_cursor") or "")
            if not int(page.get("has_more") or 0):
                return messages, next_cursor or current_cursor
            if not next_cursor or next_cursor == current_cursor:
                raise WeComKfError("sync_msg 分页游标未前进")
            current_cursor = next_cursor
        raise WeComKfError("sync_msg 分页超过安全上限")

    def send_text(
        self,
        external_userid: str,
        open_kfid: str,
        content: str,
        source_msgid: str,
    ) -> dict[str, Any]:
        return self._post(
            "/kf/send_msg",
            {
                "touser": external_userid,
                "open_kfid": open_kfid,
                "msgid": reply_msgid(source_msgid),
                "msgtype": "text",
                "text": {"content": _truncate_utf8(content, 2048)},
            },
        )


def _api_error(operation: str, result: dict[str, Any]) -> WeComKfError:
    code = result.get("errcode", "unknown")
    message = str(result.get("errmsg") or "unknown")[:200]
    return WeComKfError(f"{operation} 失败: errcode={code}, errmsg={message}")


def reply_msgid(source_msgid: str) -> str:
    return hashlib.sha256(f"wecom-kf:{source_msgid}".encode("utf-8")).hexdigest()[:32]


def _truncate_utf8(value: str, limit: int) -> str:
    data = value.encode("utf-8")
    if len(data) <= limit:
        return value
    return data[:limit].decode("utf-8", errors="ignore")


def build_reply(message: dict[str, Any], config: WeComKfConfig) -> str:
    content = str((message.get("text") or {}).get("content") or "").strip()
    fallback = f"已收到：{content}"
    if not config.processor_url:
        return fallback
    payload = {
        "model": "browser-chatgpt",
        "stream": False,
        "messages": [{"role": "user", "content": content}],
        "metadata": {
            "channel": "wecom",
            "transport": "wecom_kf",
            "account_id": f"kf:{message.get('open_kfid') or ''}",
            "chat_type": "private",
            "peer_id": str(message.get("external_userid") or ""),
            "conversation_id": str(message.get("external_userid") or ""),
            "sender_id": str(message.get("external_userid") or ""),
            "message_id": str(message.get("msgid") or ""),
            "chatgpt_project": "WeCom-KF",
        },
    }
    try:
        result = _http_json(
            "POST",
            config.processor_url,
            payload,
            config.processor_timeout_seconds,
            attempts=1,
        )
        choices = result.get("choices") or []
        reply = str(((choices[0] or {}).get("message") or {}).get("content") or "").strip()
        if reply:
            return reply
        raise WeComKfError("处理器未返回文本")
    except (IndexError, TypeError, WeComKfError):
        logger.exception("wecom kf processor failed; falling back to echo")
        return fallback


_client_lock = threading.Lock()
_client_key: tuple[str, str, float] | None = None
_client: WeComKfClient | None = None
_sync_lock = threading.Lock()
_cursors: dict[str, str] = {}


def client_for_config(config: WeComKfConfig) -> WeComKfClient:
    global _client, _client_key
    key = (config.corp_id, config.app_secret, config.api_timeout_seconds)
    with _client_lock:
        if _client is None or _client_key != key:
            _client = WeComKfClient(*key)
            _client_key = key
        return _client


def handle_notification(
    notification: KfNotification,
    config: WeComKfConfig,
    *,
    client: WeComKfClient | None = None,
) -> None:
    active_client = client or client_for_config(config)
    with _sync_lock:
        cursor = _cursors.get(notification.open_kfid, "")
        try:
            messages, next_cursor = active_client.sync_messages(
                notification.open_kfid, notification.sync_token, cursor
            )
            for message in messages:
                if int(message.get("origin") or 0) != 3 or message.get("msgtype") != "text":
                    continue
                source_msgid = str(message.get("msgid") or "").strip()
                external_userid = str(message.get("external_userid") or "").strip()
                open_kfid = str(message.get("open_kfid") or notification.open_kfid).strip()
                if not source_msgid or not external_userid or not open_kfid:
                    continue
                reply = build_reply({**message, "open_kfid": open_kfid}, config)
                active_client.send_text(external_userid, open_kfid, reply, source_msgid)
            _cursors[notification.open_kfid] = next_cursor
        except Exception:
            # 不记录消息正文、临时 sync token 或 access_token。
            logger.exception("wecom kf background processing failed")


def crypto_for_config(config: WeComKfConfig) -> WeComKfCrypto:
    return WeComKfCrypto(
        config.callback_token,
        config.callback_aes_key,
        config.corp_id,
    )
