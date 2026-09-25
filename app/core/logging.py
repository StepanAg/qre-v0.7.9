"""Unified logging with context ids and secret redaction."""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re
from typing import Iterator

CONTEXT_FIELDS = ("correlation_id", "signal_id", "order_id", "exec_id", "trade_id")
_ctx: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("qre_log_ctx", default={})

# key=value / "key": "value" patterns for secret-like names
_SECRET_KV = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|secret|token|password|passwd|signature|x-bapi-sign)"
    r"([\"']?\s*[=:]\s*[\"']?)([^\s\"',}]+)"
)
_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Any registered value is masked wherever it appears in a log line."""
    if value and len(value) >= 4:
        _registered_secrets.add(value)


def redact(text: str) -> str:
    for s in _registered_secrets:
        text = text.replace(s, "***")
    return _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _ctx.get()
        record.ctx = " ".join(f"{k}={ctx[k]}" for k in CONTEXT_FIELDS if k in ctx)
        return True


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


@contextlib.contextmanager
def log_context(**ids: str) -> Iterator[None]:
    unknown = set(ids) - set(CONTEXT_FIELDS)
    if unknown:
        raise ValueError(f"unknown log context fields: {sorted(unknown)}")
    token = _ctx.set({**_ctx.get(), **{k: v for k, v in ids.items() if v}})
    try:
        yield
    finally:
        _ctx.reset(token)


def setup_logging(level: str = "INFO", stream=None) -> logging.Logger:
    root = logging.getLogger("qre")
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream)
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s [%(ctx)s] %(message)s")
    )
    root.addHandler(handler)
    root.propagate = False
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"qre.{name}")


LOG_ROTATE_BYTES = 10 * 1024 * 1024     # rotate a log file at 10 MB ...
LOG_ROTATE_BACKUPS = 5                  # ... keeping 5 old files (bounded disk use for 24/7 runs)


def add_file_handler(path, *, max_bytes: int = LOG_ROTATE_BYTES, backups: int = LOG_ROTATE_BACKUPS
                     ) -> logging.Handler:
    """Also write qre logs to a size-rotated file (same context + redaction as the console)."""
    from logging.handlers import RotatingFileHandler
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    h.addFilter(ContextFilter())
    h.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s [%(ctx)s] %(message)s"))
    logging.getLogger("qre").addHandler(h)
    return h
