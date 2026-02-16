"""Structured JSON logging with rotating file handler.

Usage:
    from src.logger import get_logger
    log = get_logger(__name__)
    log.info("scan complete", extra={"universe": 320, "tradeable": 14})
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JSONFormatter(logging.Formatter):
    """Emit each record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge extra keys (skip internal LogRecord attrs)
        skip = set(logging.LogRecord.__dict__) | {
            "message",
            "asctime",
            "args",
            "msg",
            "stack_info",
            "exc_info",
            "exc_text",
        }
        for k, v in record.__dict__.items():
            if k not in skip:
                payload[k] = v
        if record.exc_info and not record.exc_text:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_CONFIGURED = False


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Initialise root logger.  Safe to call multiple times (idempotent)."""
    global _CONFIGURED  # noqa: PLW0603
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # ── Stdout handler (JSON) ──
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(JSONFormatter())
    root.addHandler(sh)

    # ── Rotating file handler ──
    if log_file:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(p),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(JSONFormatter())
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    """Return a named child logger."""
    return logging.getLogger(name)
