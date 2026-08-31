"""Structured JSON logging setup."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


class JsonFormatter(logging.Formatter):
    """Render each log record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", logfile: Optional[Path] = None) -> None:
    """Configure the root logger with JSON output to stdout and optional file."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter())
    root.addHandler(stream)
    if logfile is not None:
        logfile = Path(logfile)
        logfile.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(logfile), encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)


def log_event(logger: logging.Logger, msg: str, **fields: Any) -> None:
    """Emit a structured event with additional fields via logging extra."""
    logger.info(msg, extra=fields)
