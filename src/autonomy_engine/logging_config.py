"""Structured JSON logging.

One request = one JSON log line, so a Lambda's CloudWatch stream can be queried
by field (session_id, routing_decision, latency_ms) instead of grepped.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Final

#: Standard LogRecord attributes to exclude when flattening `extra=...` fields.
_RESERVED_ATTRS: Final[frozenset[str]] = frozenset(
    logging.LogRecord(
        name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__.keys()
) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Renders each LogRecord as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger, idempotently.

    Safe to call more than once (e.g. once at module import and once from a
    test fixture) -- existing handlers are cleared first so log lines are never
    duplicated.
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
