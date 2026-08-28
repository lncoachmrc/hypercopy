from __future__ import annotations

import io
import json
import logging

from app.core import logging as app_logging


def _payload(name: str, level: int, message: str, **extra):
    record = logging.LogRecord(name, level, __file__, 1, message, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(app_logging.JsonFormatter().format(record))


def test_uvicorn_websocket_info_events_have_benign_structured_codes():
    accepted = _payload(
        'uvicorn.error',
        logging.INFO,
        "('127.0.0.1', 1234) - \"WebSocket /api/ws\" [accepted]",
    )
    opened = _payload('uvicorn.error', logging.INFO, 'connection open')

    assert accepted['severity'] == 'INFO'
    assert accepted['event_code'] == 'UVICORN_WEBSOCKET_ACCEPTED'
    assert opened['severity'] == 'INFO'
    assert opened['event_code'] == 'UVICORN_WEBSOCKET_OPEN'


def test_uvicorn_real_error_keeps_error_severity():
    payload = _payload('uvicorn.error', logging.ERROR, 'ASGI application failure')

    assert payload['severity'] == 'ERROR'
    assert payload['level'] == 'ERROR'
    assert payload['event_code'] == 'UVICORN_ERROR'


def test_operational_fallback_events_get_stable_codes_and_safe_extras():
    consumed = _payload(
        'app.workers.resilient_execution_worker',
        logging.INFO,
        'Consuming copy job through PostgreSQL fallback',
        job_id='job-1',
    )
    activated = _payload(
        'app.workers.execution_worker',
        logging.WARNING,
        'Full reconciliation failed; refreshing follower observability only',
        master_network='mainnet',
        follower_network='testnet',
    )

    assert consumed['severity'] == 'INFO'
    assert consumed['event_code'] == 'POSTGRES_FALLBACK_JOB_CONSUMED'
    assert consumed['job_id'] == 'job-1'
    assert activated['severity'] == 'WARNING'
    assert activated['event_code'] == 'RECONCILIATION_FALLBACK_ACTIVATED'
    assert activated['master_network'] == 'mainnet'
    assert activated['follower_network'] == 'testnet'


def test_explicit_event_code_is_preserved():
    payload = _payload(
        'app.example',
        logging.WARNING,
        'explicit event',
        event_code='EXPLICIT_OPERATIONAL_EVENT',
    )

    assert payload['event_code'] == 'EXPLICIT_OPERATIONAL_EVENT'
    assert payload['severity'] == 'WARNING'


def test_configure_logging_routes_uvicorn_to_structured_stdout_without_duplicates(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(app_logging.sys, 'stdout', stream)

    names = [None, 'uvicorn', 'uvicorn.error', 'uvicorn.access']
    snapshots = {}
    for name in names:
        logger = logging.getLogger() if name is None else logging.getLogger(name)
        snapshots[name] = (list(logger.handlers), logger.level, logger.propagate)

    try:
        app_logging.configure_logging()
        app_logging.configure_logging()

        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert root.handlers[0].stream is stream
        assert isinstance(root.handlers[0].formatter, app_logging.JsonFormatter)

        for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
            logger = logging.getLogger(name)
            assert len(logger.handlers) == 1
            assert logger.handlers[0].stream is stream
            assert isinstance(logger.handlers[0].formatter, app_logging.JsonFormatter)
            assert logger.propagate is False

        uvicorn_error = logging.getLogger('uvicorn.error')
        old_level = uvicorn_error.level
        uvicorn_error.setLevel(logging.INFO)
        try:
            uvicorn_error.info('connection open')
        finally:
            uvicorn_error.setLevel(old_level)

        lines = [line for line in stream.getvalue().splitlines() if line]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload['severity'] == 'INFO'
        assert payload['event_code'] == 'UVICORN_WEBSOCKET_OPEN'
    finally:
        for name in names:
            logger = logging.getLogger() if name is None else logging.getLogger(name)
            handlers, level, propagate = snapshots[name]
            logger.handlers.clear()
            logger.handlers.extend(handlers)
            logger.setLevel(level)
            logger.propagate = propagate
