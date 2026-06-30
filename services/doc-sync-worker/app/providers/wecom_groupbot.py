from __future__ import annotations

import json
from typing import Any

import requests

try:
    import websocket  # websocket-client
except ModuleNotFoundError:  # pragma: no cover - 仅在未装依赖的纯单测环境
    websocket = None  # type: ignore[assignment]


WS_URL = "wss://openws.work.weixin.qq.com"


class WeComGroupBotClient:
    """企业微信智能机器人长连接客户端（接收群@消息 + 回复）。

    长连接用 bot_id + secret 直接订阅鉴权（无单独 access_token）。
    """

    def __init__(self, bot_id: str, secret: str, *, timeout: int = 10) -> None:
        self.bot_id = bot_id
        self.secret = secret
        self.timeout = timeout
        self.ws: Any = None
        self._req_seq = 0

    def _req_id(self) -> str:
        self._req_seq += 1
        return f"r{self._req_seq}"

    def connect(self) -> dict[str, Any]:
        if websocket is None:
            raise RuntimeError("缺少 websocket-client，请安装 services/doc-sync-worker/requirements.txt。")
        self.ws = websocket.create_connection(WS_URL, timeout=self.timeout)
        self.ws.send(
            json.dumps(
                {
                    "cmd": "aibot_subscribe",
                    "headers": {"req_id": self._req_id()},
                    "body": {"bot_id": self.bot_id, "secret": self.secret},
                }
            )
        )
        ack = json.loads(self.ws.recv())
        if ack.get("errcode") not in (0, None):
            raise RuntimeError(f"aibot_subscribe failed: {ack}")
        self.ws.settimeout(self.timeout)
        return ack

    def ping(self) -> None:
        if self.ws is None:
            return
        self.ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": self._req_id()}}))

    def recv(self) -> dict[str, Any] | None:
        """阻塞接收一帧；超时返回 None。其它异常向上抛由调用方处理重连。"""
        if self.ws is None:
            raise RuntimeError("未连接")
        try:
            raw = self.ws.recv()
        except websocket.WebSocketTimeoutException:  # type: ignore[union-attr]
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def reply(self, response_url: str, text: str) -> dict[str, Any]:
        """用消息回调里的 response_url 被动回复一条文本（1 小时内有效，不计主动发限额）。"""
        resp = requests.post(
            response_url,
            json={"msgtype": "text", "text": {"content": text}},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {}

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None
