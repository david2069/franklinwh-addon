"""In-memory rolling log buffer — captures Python log records for the Logs UI tab.

Attach to the root logger at startup via ``attach()``. The last N records are
kept in a deque and exposed via ``get_recent()``.
"""
import logging
from collections import deque
from datetime import datetime, timezone

_BUFFER: deque = deque(maxlen=500)  # last 500 records


class _BufferHandler(logging.Handler):
    """Appends formatted records to the shared in-memory deque."""

    INTERESTING_LOGGERS = {
        "src.services.gateway_service",
        "src.services.gateway_registry",
        "src.services.mqtt_publisher",
        "src.services.mqtt_listener",
        "src.routes.api_gateways",
        "src.main",
        "franklinwh_cloud",
    }

    def emit(self, record: logging.LogRecord) -> None:
        # Only buffer records from loggers we care about (avoid noise)
        name = record.name
        if not any(name.startswith(n) for n in self.INTERESTING_LOGGERS):
            return
        _BUFFER.append({
            "ts":      datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":   record.levelname,       # DEBUG / INFO / WARNING / ERROR / CRITICAL
            "logger":  record.name,
            "message": record.getMessage(),
            "exc":     self.formatException(record.exc_info) if record.exc_info else None,
        })


_handler: _BufferHandler | None = None


def attach() -> None:
    """Install the buffer handler on the root logger (idempotent)."""
    global _handler
    if _handler is not None:
        return
    _handler = _BufferHandler()
    _handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(_handler)


def get_recent(limit: int = 200, level: str | None = None) -> list[dict]:
    """Return the most recent log records, newest-last.

    :param limit: Max records to return (default 200).
    :param level: Optional filter — "ERROR", "WARNING", "INFO", etc.
    """
    records = list(_BUFFER)
    if level:
        records = [r for r in records if r["level"] == level.upper()]
    return records[-limit:]
