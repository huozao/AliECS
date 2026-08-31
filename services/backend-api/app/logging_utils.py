"""Structured JSON logging helpers.

Keeps stdlib `logging` but renders one JSON object per line so log
aggregation (or plain `journalctl` greps) can filter by field instead of
parsing free-text messages.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import IO

# 先输出的常用字段，只决定 key 顺序，不再决定「哪些字段能被输出」。
_CORE_FIELDS = (
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "user",
    "route",
)

# LogRecord 自带的属性。凡不在这里、又是调用方经 log_event(**fields) 显式传进来的，
# 一律输出——原来这里是个固定白名单，`log_event` 签名收任意字段、formatter 却只认
# 七个，多余的**静默丢弃**。2026-08-31 踩到：422 处理器把出错字段路径记进 `fields`，
# 自测时用自带 format 的 handler 看着一切正常，生产那行 JSON 里根本没有 `fields`。
# 「记了」和「输出了」是两件事，白名单让它们在观测面上长得一样。
_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None))
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _CORE_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        for key, value in vars(record).items():
            if key in _STANDARD_RECORD_ATTRS or key in payload or value is None:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # default=str：结构化字段可能带 datetime 之类，宁可降级成字符串也不要
        # 让一行日志因为序列化失败而整条丢掉。
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(name: str = "aliecs", stream: IO[str] | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, message: str, **fields: object) -> None:
    logger.info(message, extra=fields)
