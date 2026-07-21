"""微信客服 API：文本对话与资料任务入口。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import threading
import time
import xml.etree.ElementTree as ET

from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

from Crypto.Cipher import AES

from app.integrations import product_center
from app.integrations.wecom_kf_tasks import (
    DownloadedMedia,
    KfTaskCoordinator,
    PostgresKfTaskStore,
    ProcessorAttachment,
    TaskOutcome,
)


WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
TOKEN_ERROR_CODES = {40014, 42001}
# 95033=repeated msgid：该回复此前已成功下发，属幂等成功而非失败。
SEND_IDEMPOTENT_CODES = frozenset({95033})
MAX_CALLBACK_BYTES = 1024 * 1024
MAX_SYNC_PAGES = 100
MAX_MEDIA_BYTES = 20 * 1024 * 1024

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

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        ok_codes: frozenset[int] = frozenset(),
    ) -> dict[str, Any]:
        for attempt in range(2):
            token = self.access_token(force_refresh=attempt == 1)
            query = parse.urlencode({"access_token": token})
            result = _http_json(
                "POST", f"{WECOM_API_BASE}{path}?{query}", payload, self._timeout
            )
            errcode = int(result.get("errcode", -1))
            if errcode in TOKEN_ERROR_CODES and attempt == 0:
                continue
            if errcode != 0 and errcode not in ok_codes:
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
        *,
        purpose: str = "reply",
    ) -> dict[str, Any]:
        return self._post(
            "/kf/send_msg",
            {
                "touser": external_userid,
                "open_kfid": open_kfid,
                "msgid": reply_msgid(source_msgid, purpose),
                "msgtype": "text",
                "text": {"content": _truncate_utf8(content, 2048)},
            },
            ok_codes=SEND_IDEMPOTENT_CODES,
        )

    def send_menu(
        self,
        external_userid: str,
        open_kfid: str,
        content: str,
        source_msgid: str,
        task_key: str,
        *,
        purpose: str = "task_review",
    ) -> dict[str, Any]:
        if not task_key or len(task_key) != 32:
            raise WeComKfError("资料任务标识格式错误")
        return self._post(
            "/kf/send_msg",
            {
                "touser": external_userid,
                "open_kfid": open_kfid,
                "msgid": reply_msgid(source_msgid, purpose),
                "msgtype": "msgmenu",
                "msgmenu": {
                    "head_content": _truncate_utf8(content, 1024),
                    "list": [
                        {
                            "type": "click",
                            "click": {
                                "id": f"kf_confirm_{task_key}",
                                "content": "确认处理",
                            },
                        },
                        {
                            "type": "click",
                            "click": {
                                "id": f"kf_supplement_{task_key}",
                                "content": "补充资料",
                            },
                        },
                        {
                            "type": "click",
                            "click": {
                                "id": f"kf_cancel_{task_key}",
                                "content": "取消任务",
                            },
                        },
                    ],
                    "tail_content": "也可直接发送以上文字指令。",
                },
            },
            ok_codes=SEND_IDEMPOTENT_CODES,
        )

    def download_media(self, media_id: str) -> DownloadedMedia:
        if not media_id:
            raise WeComKfError("media_id 为空")
        for attempt in range(2):
            token = self.access_token(force_refresh=attempt == 1)
            query = parse.urlencode({"access_token": token, "media_id": media_id})
            req = request.Request(f"{WECOM_API_BASE}/media/get?{query}", method="GET")
            try:
                with request.urlopen(req, timeout=self._timeout) as response:
                    body = response.read(MAX_MEDIA_BYTES + 1)
                    content_type = str(response.headers.get("Content-Type") or "")
                    disposition = str(response.headers.get("Content-Disposition") or "")
            except error.HTTPError as exc:
                raise WeComKfError(f"下载客服附件 HTTP {exc.code}") from exc
            except (error.URLError, TimeoutError, OSError) as exc:
                raise WeComKfError("下载客服附件连接失败或超时") from exc
            if len(body) > MAX_MEDIA_BYTES:
                raise WeComKfError("客服附件超过 20MB 限制")
            api_result = _json_object_or_none(body, content_type)
            if api_result is not None and "errcode" in api_result:
                errcode = int(api_result.get("errcode", -1))
                if errcode in TOKEN_ERROR_CODES and attempt == 0:
                    continue
                raise _api_error("media/get", api_result)
            mime_type = content_type.split(";", 1)[0].strip().lower()
            if not mime_type or mime_type == "application/octet-stream":
                mime_type = "application/octet-stream"
            filename = _content_disposition_filename(disposition) or (
                f"attachment-{media_id[:12]}"
            )
            return DownloadedMedia(body, mime_type, filename)
        raise WeComKfError("media/get access_token 刷新后仍失败")


def _api_error(operation: str, result: dict[str, Any]) -> WeComKfError:
    code = result.get("errcode", "unknown")
    message = str(result.get("errmsg") or "unknown")[:200]
    return WeComKfError(f"{operation} 失败: errcode={code}, errmsg={message}")


def reply_msgid(source_msgid: str, purpose: str = "reply") -> str:
    suffix = "" if purpose == "reply" else f":{purpose}"
    digest = hashlib.sha256(f"wecom-kf:{source_msgid}{suffix}".encode("utf-8"))
    return digest.hexdigest()[:32]


def _json_object_or_none(body: bytes, content_type: str) -> dict[str, Any] | None:
    if "json" not in content_type.lower() and not body.lstrip().startswith(b"{"):
        return None
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return result if isinstance(result, dict) else None


def _content_disposition_filename(value: str) -> str:
    extended = re.search(r"filename\*=UTF-8''([^;]+)", value, re.IGNORECASE)
    if extended:
        return parse.unquote(extended.group(1)).strip()
    regular = re.search(r'filename="?([^";]+)', value, re.IGNORECASE)
    return regular.group(1).strip() if regular else ""


def _truncate_utf8(value: str, limit: int) -> str:
    data = value.encode("utf-8")
    if len(data) <= limit:
        return value
    return data[:limit].decode("utf-8", errors="ignore")


def call_processor(
    message: dict[str, Any],
    config: WeComKfConfig,
    content: str,
    attachments: list[ProcessorAttachment] | None = None,
) -> str:
    if not config.processor_url:
        raise WeComKfError("未配置微信客服处理器")
    user_content: str | list[dict[str, Any]] = content
    if attachments:
        user_content = [{"type": "text", "text": content}]
        user_content.extend(
            {"type": "image_url", "image_url": {"url": item.data_url}}
            for item in attachments
        )
    payload = {
        "model": "browser-chatgpt",
        "stream": False,
        "messages": [{"role": "user", "content": user_content}],
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
    result = _http_json(
        "POST",
        config.processor_url,
        payload,
        config.processor_timeout_seconds,
        attempts=1,
    )
    choices = result.get("choices") or []
    try:
        reply = str(((choices[0] or {}).get("message") or {}).get("content") or "").strip()
    except (IndexError, TypeError) as exc:
        raise WeComKfError("处理器返回格式错误") from exc
    if not reply:
        raise WeComKfError("处理器未返回文本")
    return reply


def build_reply(message: dict[str, Any], config: WeComKfConfig) -> str:
    content = str((message.get("text") or {}).get("content") or "").strip()
    fallback = f"已收到：{content}"
    if not config.processor_url:
        return fallback
    try:
        return call_processor(message, config, content)
    except WeComKfError:
        logger.exception("wecom kf processor failed; falling back to echo")
        return fallback


_client_lock = threading.Lock()
_client_key: tuple[str, str, float] | None = None
_client: WeComKfClient | None = None
_sync_lock = threading.Lock()
_cursors: dict[str, str] = {}
_task_store_lock = threading.Lock()
_task_store_key: tuple[str, str] | None = None
_task_store: PostgresKfTaskStore | None = None


def client_for_config(config: WeComKfConfig) -> WeComKfClient:
    global _client, _client_key
    key = (config.corp_id, config.app_secret, config.api_timeout_seconds)
    with _client_lock:
        if _client is None or _client_key != key:
            _client = WeComKfClient(*key)
            _client_key = key
        return _client


def task_store_for_env() -> PostgresKfTaskStore | None:
    global _task_store, _task_store_key
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return None
    storage_root = os.getenv(
        "WECOM_KF_TASK_STORAGE_DIR", "/app/wecom-kf-materials"
    ).strip()
    key = (database_url, storage_root)
    with _task_store_lock:
        if _task_store is None or _task_store_key != key:
            _task_store = PostgresKfTaskStore(database_url, storage_root)
            _task_store_key = key
        return _task_store


def _send_task_outcome(
    client: WeComKfClient,
    store: PostgresKfTaskStore,
    outcome: TaskOutcome,
    message: dict[str, Any],
) -> None:
    if not outcome.reply_type:
        return
    source_msgid = str(message["msgid"])
    open_kfid = str(message["open_kfid"])
    external_userid = str(message["external_userid"])
    outbound_msgid = reply_msgid(source_msgid, outcome.purpose)
    store.record_outbound(
        outbound_msgid,
        outcome.task_id,
        open_kfid,
        external_userid,
        outcome.purpose,
    )
    if outcome.reply_type == "menu":
        client.send_menu(
            external_userid,
            open_kfid,
            outcome.text,
            source_msgid,
            outcome.task_key,
            purpose=outcome.purpose,
        )
    else:
        client.send_text(
            external_userid,
            open_kfid,
            outcome.text,
            source_msgid,
            purpose=outcome.purpose,
        )
    store.mark_outbound_sent(outbound_msgid)


def _build_archive_external(
    store: PostgresKfTaskStore | None,
) -> Any:
    """构造确认归档后的外部同步回调；未配置或未启用时返回 None（退化为仅本地归档）。"""
    if store is None:
        return None
    config = product_center.ProductCenterConfig.from_env()
    if not config.enabled:
        return None

    def _archive(task: dict[str, Any], items: list[dict[str, Any]]) -> Any:
        store.set_external_archive_status(int(task["id"]), "pending")
        try:
            result = product_center.archive_materials(
                config, task, items, store.original_bytes
            )
        except Exception as exc:  # 不让外部故障影响本地归档主流程。
            logger.warning("资料任务外部归档异常: %s", exc)
            store.set_external_archive_status(int(task["id"]), "failed")
            raise
        try:
            store.apply_archive_result(int(task["id"]), result)
        except Exception as exc:
            logger.warning("外部归档结果落库失败: %s", exc)
        return result

    return _archive


def _process_message(
    message: dict[str, Any],
    notification: KfNotification,
    config: WeComKfConfig,
    active_client: WeComKfClient,
    active_store: PostgresKfTaskStore | None,
    coordinator: KfTaskCoordinator | None,
) -> None:
    if message.get("msgtype") == "event":
        event = message.get("event") or {}
        if active_store and event.get("event_type") == "msg_send_fail":
            failed_msgid = str(event.get("fail_msgid") or "").strip()
            if failed_msgid:
                active_store.mark_outbound_failed(
                    failed_msgid, int(event.get("fail_type") or 0)
                )
        return
    if int(message.get("origin") or 0) != 3:
        return
    source_msgid = str(message.get("msgid") or "").strip()
    external_userid = str(message.get("external_userid") or "").strip()
    open_kfid = str(message.get("open_kfid") or notification.open_kfid).strip()
    if not source_msgid or not external_userid or not open_kfid:
        return
    normalized = {**message, "open_kfid": open_kfid}
    if coordinator:
        outcome = coordinator.handle(
            normalized,
            download_media=active_client.download_media,
            analyze=lambda prompt, attachments: call_processor(
                normalized, config, prompt, attachments
            ),
            archive_external=_build_archive_external(active_store),
        )
        if outcome and outcome.handled:
            _send_task_outcome(active_client, active_store, outcome, normalized)
            return
    if message.get("msgtype") == "text":
        # 文本兜底路径幂等：回复 ID 由源 msgid 确定性派生；重新部署后从空游标
        # 回放 backlog 时，跳过此前已成功回复的旧消息，避免重复调用处理器与重复回复。
        outbound_msgid = reply_msgid(source_msgid)
        if active_store and active_store.outbound_already_sent(outbound_msgid):
            return
        reply = build_reply(normalized, config)
        if active_store:
            active_store.record_outbound(
                outbound_msgid, None, open_kfid, external_userid, "reply"
            )
        active_client.send_text(external_userid, open_kfid, reply, source_msgid)
        if active_store:
            active_store.mark_outbound_sent(outbound_msgid)


def handle_notification(
    notification: KfNotification,
    config: WeComKfConfig,
    *,
    client: WeComKfClient | None = None,
    task_store: PostgresKfTaskStore | None = None,
) -> None:
    active_client = client or client_for_config(config)
    active_store = task_store or task_store_for_env()
    coordinator = KfTaskCoordinator(active_store) if active_store else None
    with _sync_lock:
        try:
            cursor = (
                active_store.get_cursor(notification.open_kfid)
                if active_store
                else _cursors.get(notification.open_kfid, "")
            )
            messages, next_cursor = active_client.sync_messages(
                notification.open_kfid, notification.sync_token, cursor
            )
            for message in messages:
                # 单条消息独立容错：一条失败只记日志并跳过，绝不中止整批，
                # 否则游标无法推进，后续消息（含用户真正的指令）会被永久卡死。
                try:
                    _process_message(
                        message,
                        notification,
                        config,
                        active_client,
                        active_store,
                        coordinator,
                    )
                except Exception:
                    logger.exception("wecom kf single message processing failed")
            if active_store:
                active_store.set_cursor(notification.open_kfid, next_cursor)
            else:
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
