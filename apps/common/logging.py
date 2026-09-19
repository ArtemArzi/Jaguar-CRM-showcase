from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

_log_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "log_context",
    default={},
)

SENSITIVE_FIELD_RE = re.compile(
    r"(password|passwd|pwd|secret|token|authorization|cookie|session|csrf|pin|phone|email|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
TOKEN_RE = re.compile(r"(?i)\b(bearer|token|refresh|access|password|secret|authorization|cookie)(?:=|:|\s)+[^\s,;]+")
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
PHONE_RE = re.compile(r"(?<!\d)\+?\d[\d\s().-]{7,}\d(?!\d)")

RESERVED_LOG_RECORD_KEYS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
    }
)


def set_log_context(**values: Any) -> contextvars.Token:
    return _log_context.set({k: v for k, v in values.items() if v is not None})


def clear_log_context(token: contextvars.Token | None = None) -> None:
    if token is None:
        _log_context.set({})
        return
    _log_context.reset(token)


def get_log_context() -> dict[str, Any]:
    return dict(_log_context.get())


def hash_for_log(value: str, *, salt: str) -> str:
    digest = hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()
    return digest[:16]


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": _redact_value(record.getMessage()),
            "service": os.getenv("SERVICE_NAME", "crm-jaguar"),
            "release": os.getenv("RELEASE_SHA", ""),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "process": record.process,
        }
        payload.update(_sanitize_mapping(get_log_context()))
        payload.update(_record_extra(record))

        if record.exc_info:
            payload["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": _redact_value(str(record.exc_info[1])),
                "stack": _redact_value(self.formatException(record.exc_info)),
            }
        if record.stack_info:
            payload["stack"] = _redact_value(self.formatStack(record.stack_info))

        return json.dumps(payload, ensure_ascii=False, default=_json_default, separators=(",", ":"))


class SafeTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original_msg = record.msg
        original_args = record.args
        record.msg = _redact_value(record.getMessage())
        record.args = ()
        try:
            return super().format(record)
        finally:
            record.msg = original_msg
            record.args = original_args


def _record_extra(record: logging.LogRecord) -> dict[str, Any]:
    return _sanitize_mapping(
        {
            key: value
            for key, value in record.__dict__.items()
            if key not in RESERVED_LOG_RECORD_KEYS and not key.startswith("_")
        }
    )


def _sanitize_mapping(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _redact_by_key(key, value) for key, value in values.items()}


def _redact_by_key(key: str, value: Any) -> Any:
    if SENSITIVE_FIELD_RE.search(key):
        return "[redacted]"
    return _redact_value(value)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _sanitize_mapping(value)
    if isinstance(value, list | tuple | set):
        return [_redact_value(item) for item in value]
    if not isinstance(value, str):
        return value

    safe = CONTROL_CHARS_RE.sub("", value).replace("\r", "\\r").replace("\n", "\\n")
    safe = TOKEN_RE.sub(lambda match: f"{match.group(1)}=[redacted]", safe)
    safe = EMAIL_RE.sub("[redacted-email]", safe)
    safe = PHONE_RE.sub("[redacted-phone]", safe)
    return safe


def _json_default(value: Any) -> str | int | float | bool | None:
    if isinstance(value, Decimal):
        return str(value)
    return str(value)
