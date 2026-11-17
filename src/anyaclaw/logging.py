"""Structured logging with request/session correlation.

Every log record can carry a ``trace_id`` (one agent turn) and a ``session`` key,
injected through :mod:`contextvars` so deeply nested tools do not need to pass
them around. Output is JSON by default, which makes logs greppable and ready for
Loki/ELK, while ``log_json = false`` keeps a human-friendly console format.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "configure_logging",
    "current_trace",
    "get_logger",
    "redact",
    "set_trace",
    "trace_scope",
]

_trace_id: ContextVar[str] = ContextVar("anyaclaw_trace_id", default="")
_session_key: ContextVar[str] = ContextVar("anyaclaw_session_key", default="")

_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}"
    r"|Bearer\s+[A-Za-z0-9._\-]+"
    r"|(?i:api[_-]?key\")?\s*[:=]\s*\"?[A-Za-z0-9_\-]{16,}\"?)"
)


def redact(text: str) -> str:
    """Mask anything that looks like an API key or bearer token."""
    if not text:
        return text
    return _SECRET_PATTERN.sub("***", text)


def set_trace(trace_id: str, session_key: str = "") -> None:
    _trace_id.set(trace_id)
    if session_key:
        _session_key.set(session_key)


def current_trace() -> str:
    return _trace_id.get()


class _TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.trace_id = getattr(record, "trace_id", "") or _trace_id.get()
        record.session_key = getattr(record, "session_key", "") or _session_key.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if getattr(record, "trace_id", ""):
            payload["trace_id"] = record.trace_id
        if getattr(record, "session_key", ""):
            payload["session"] = record.session_key
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update({key: _safe(value) for key, value in extra.items()})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Compact, colour-free console format."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        trace = f" [{record.trace_id[:8]}]" if getattr(record, "trace_id", "") else ""
        message = redact(record.getMessage())
        line = f"{stamp} {record.levelname[:4]:<4} {record.name}{trace}: {message}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else redact(value)
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    return redact(str(value))


def configure_logging(
    level: str = "INFO",
    *,
    json_output: bool = True,
    log_file: str | os.PathLike[str] | None = None,
) -> None:
    """Install the Anyaclaw log handler on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    for handler in list(root.handlers):
        if getattr(handler, "_anyaclaw", False):
            root.removeHandler(handler)
            handler.close()

    handler: logging.Handler
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    handler.addFilter(_TraceFilter())
    handler._anyaclaw = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    # Third-party noise stays at WARNING unless we are debugging.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"anyaclaw.{name}")


class trace_scope:
    """Context manager that scopes a trace id and restores the previous one."""

    def __init__(self, trace_id: str, session_key: str = "") -> None:
        self._trace_id = trace_id
        self._session_key = session_key
        self._tokens: list[Any] = []

    def __enter__(self) -> trace_scope:
        self._tokens.append(_trace_id.set(self._trace_id))
        if self._session_key:
            self._tokens.append(_session_key.set(self._session_key))
        return self

    def __exit__(self, *exc_info: Any) -> None:
        while self._tokens:
            token = self._tokens.pop()
            if token.var is _trace_id:  # type: ignore[attr-defined]
                _trace_id.reset(token)
            else:
                _session_key.reset(token)
