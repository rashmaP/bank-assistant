"""Structured logging with per-request correlation IDs (shared by both services).

* One JSON object per line (LOG_FORMAT=json, default) or readable text (LOG_FORMAT=text).
* `request_id` is stored in a contextvar, so every log line emitted while handling a request,
  including inside worker threads and the agent, carries it automatically.
* The assistant forwards the ID to the bank (X-Request-ID), so one customer request can be
  traced across both services.
* Never log message text, amounts or credentials. Log tool names, outcomes, counts, timings.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def current_request_id() -> str:
    return _request_id.get()


def set_request_id(rid: str):
    return _request_id.set(rid)


def reset_request_id(token) -> None:
    _request_id.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def accept_request_id(candidate: str | None) -> str:
    """Reuse a caller-supplied ID only if it is clean (prevents log injection); else mint one."""
    return candidate if candidate and _VALID_ID.match(candidate) else new_request_id()


def logf(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    """Log an event with structured fields: logf(log, logging.INFO, "turn", ms=12, tools=[...])."""
    logger.log(level, msg, extra={"fields": fields})


class _Formatter(logging.Formatter):
    def __init__(self, as_json: bool):
        super().__init__()
        self.as_json = as_json

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None) or {}
        base = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": current_request_id(),
        }
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        if self.as_json:
            return json.dumps({**fields, **base}, default=str, ensure_ascii=False)
        extras = " ".join(f"{k}={v}" for k, v in fields.items())
        line = f"{base['ts']} {base['level']:7} {base['logger']} [{base['request_id']}] {base['msg']} {extras}".rstrip()
        return line + ("\n" + base["exc"] if "exc" in base else "")


def setup_logging() -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter(os.environ.get("LOG_FORMAT", "json").lower() == "json"))
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    for noisy in ("httpx", "httpx2", "httpcore", "uvicorn.access"):  # our request log replaces these
        logging.getLogger(noisy).setLevel(logging.WARNING)
