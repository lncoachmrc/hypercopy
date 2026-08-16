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


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            'ts': datetime.now(UTC).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': redact(record.getMessage()),
            'correlation_id': correlation_id_var.get(),
            'service': getattr(record, 'service', None),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in logging.LogRecord(None, 0, '', 0, '', (), None).__dict__}
        for key in ('user_id', 'job_id', 'execution_id', 'asset', 'state', 'holder', 'fencing_token'):
            if key in extras:
                payload[key] = redact(extras[key], key)
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
