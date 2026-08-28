from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar('correlation_id', default='-')

_SECRET_KEYS = re.compile(r'(private.?key|secret|password|authorization|signature|ciphertext|wrapped.?dek|session)', re.I)
_HEX_PRIVATE = re.compile(r'\b0x[a-fA-F0-9]{64}\b')
_OPERATIONAL_EVENT_CODES = {
    'Consuming copy job through PostgreSQL fallback': 'POSTGRES_FALLBACK_JOB_CONSUMED',
    'Redis consumer group unavailable; using PostgreSQL fallback': 'REDIS_FALLBACK_ACTIVATED',
    'Worker consume loop failed; falling back to PostgreSQL': 'REDIS_CONSUME_LOOP_FAILED',
    'PostgreSQL fallback consumption failed': 'POSTGRES_FALLBACK_FAILED',
    'Full reconciliation failed; refreshing follower observability only': 'RECONCILIATION_FALLBACK_ACTIVATED',
    'Follower mids unavailable during observability refresh': 'FOLLOWER_OBSERVABILITY_MIDS_UNAVAILABLE',
    'Follower observability refresh failed': 'FOLLOWER_OBSERVABILITY_USER_REFRESH_FAILED',
    'Follower observability refresh completed': 'FOLLOWER_OBSERVABILITY_REFRESH_COMPLETED',
}
_UVICORN_LOGGERS = ('uvicorn', 'uvicorn.error', 'uvicorn.access')


def redact(value: Any, key: str = '') -> Any:
    if _SECRET_KEYS.search(key):
        return '[REDACTED]'
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = _HEX_PRIVATE.sub('[REDACTED_HEX]', value)
        if len(value) > 3000:
            value = value[:3000] + '…'
    return value


def _event_code(record: logging.LogRecord, message: str) -> str:
    explicit = getattr(record, 'event_code', None)
    if explicit:
        return str(explicit)

    if message in _OPERATIONAL_EVENT_CODES:
        return _OPERATIONAL_EVENT_CODES[message]

    if record.name.startswith('uvicorn'):
        lowered = message.strip().lower()
        if record.levelno >= logging.ERROR:
            return 'UVICORN_ERROR'
        if record.levelno >= logging.WARNING:
            return 'UVICORN_WARNING'
        if record.name == 'uvicorn.access':
            return 'UVICORN_ACCESS'
        if 'websocket' in lowered and '[accepted]' in lowered:
            return 'UVICORN_WEBSOCKET_ACCEPTED'
        if lowered == 'connection open':
            return 'UVICORN_WEBSOCKET_OPEN'
        if lowered == 'connection closed':
            return 'UVICORN_WEBSOCKET_CLOSED'
        return 'UVICORN_EVENT'

    return 'APP_EVENT'


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        payload: dict[str, Any] = {
            'ts': datetime.now(UTC).isoformat(),
            'level': record.levelname,
            'severity': record.levelname,
            'event_code': _event_code(record, message),
            'logger': record.name,
            'message': redact(message),
            'correlation_id': correlation_id_var.get(),
            'service': getattr(record, 'service', None),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in logging.LogRecord(None, 0, '', 0, '', (), None).__dict__}
        for key in (
            'user_id', 'job_id', 'execution_id', 'asset', 'state', 'holder',
            'fencing_token', 'network', 'master_network', 'follower_network',
            'follower_network_mode', 'users_refreshed',
        ):
            if key in extras:
                payload[key] = redact(extras[key], key)
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False, default=str)


def configure_logging() -> None:
    """Route application and Uvicorn logs through structured stdout JSON.

    Uvicorn installs dedicated handlers before importing the ASGI application.
    Replacing those handlers here prevents benign INFO events such as WebSocket
    acceptance/open messages from being classified as errors merely because the
    default Uvicorn handler writes to stderr. Semantic severity is preserved in
    the JSON payload; real WARNING/ERROR records remain WARNING/ERROR.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL.upper())

    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
