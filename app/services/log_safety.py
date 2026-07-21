"""Keep URL-borne capabilities and credentials out of process logs.

The application has to put short-lived CDP/VNC grants in WebSocket URLs because
browser WebSockets cannot set an Authorization header. Uvicorn logs the whole
upgrade target by default. Download clients likewise log redirect destinations,
including GitHub's signed release-asset query string, and third-party libraries
may log proxy URLs containing userinfo.

Install one LogRecord factory before startup work begins. Sanitizing the record
arguments (rather than one formatter) covers Uvicorn's preconfigured handlers,
our own root handler, and dependency loggers without changing their levels or
discarding useful host/path/status context.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit


_URL = re.compile(
    r"(?P<url>(?:(?:https?|wss?)://[^\s\"'<>]+|/[^\s\"'<>]*\?[^\s\"'<>]+))",
    re.IGNORECASE,
)


def redact_log_text(value: str) -> str:
    """Redact URL userinfo plus every query/fragment value in log text.

    Query strings are capabilities often enough that maintaining a list of
    names (``t``, ``sig``, ``jwt`` today) is the unsafe direction: a provider can
    rename one tomorrow. Paths and hosts survive so the log stays diagnostic.
    """

    def replace(match: re.Match[str]) -> str:
        raw = match.group("url")
        try:
            parts = urlsplit(raw)
        except ValueError:
            return "[redacted-url]"

        netloc = parts.netloc
        if "@" in netloc:
            # The delimiter is the final raw '@'. Percent-encoded '@' belongs to
            # userinfo and disappears with the rest of it.
            netloc = "***@" + netloc.rsplit("@", 1)[1]
        query = "REDACTED" if parts.query else ""
        fragment = "REDACTED" if parts.fragment else ""
        return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))

    return _URL.sub(replace, value)


def _safe(value):
    if isinstance(value, str):
        return redact_log_text(value)
    # httpx keeps the request target as a URL object in the logging argument
    # tuple. Waiting for Formatter.getMessage() would be too late for the record
    # factory, so normalize just URL-shaped dependency values here.
    value_type = type(value)
    if value_type.__name__ == "URL" and value_type.__module__.startswith("httpx"):
        return redact_log_text(str(value))
    if isinstance(value, tuple):
        return tuple(_safe(item) for item in value)
    if isinstance(value, Mapping):
        return {key: _safe(item) for key, item in value.items()}
    return value


def install_log_sanitizer() -> None:
    """Install once, preserving any factory the process already configured."""
    current = logging.getLogRecordFactory()
    if getattr(current, "_cloakbiz_log_sanitizer", False):
        return

    def factory(*args, **kwargs):
        record = current(*args, **kwargs)
        record.msg = _safe(record.msg)
        record.args = _safe(record.args)
        if record.exc_text:
            record.exc_text = redact_log_text(record.exc_text)
        return record

    factory._cloakbiz_log_sanitizer = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
