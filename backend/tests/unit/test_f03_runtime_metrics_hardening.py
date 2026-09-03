import pytest
from starlette.requests import Request

from app import main as main_module
from app.core.config import settings


def _request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (name.lower().encode('latin-1'), value.encode('latin-1'))
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            'type': 'http',
            'http_version': '1.1',
            'method': 'GET',
            'scheme': 'https',
            'path': '/metrics',
            'raw_path': b'/metrics',
            'query_string': b'',
            'headers': raw_headers,
            'client': ('203.0.113.10', 44321),
            'server': ('api.test', 443),
        }
    )


class _ForbiddenDbAccess:
    async def __aenter__(self):
        raise AssertionError('unauthorized metrics request reached the database')

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DbAccess:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RateLimiter:
    async def snapshot(self) -> dict:
        return {}


class _Redis:
    async def info(self, section: str) -> dict:
        assert section == 'memory'
        return {'used_memory': 123}

    async def get(self, key: str):
        return None


def test_openapi_and_docs_are_not_exposed_by_runtime_environment() -> None:
    assert main_module.app.docs_url is None
    assert main_module.app.redoc_url is None
    assert main_module.app.openapi_url is None


@pytest.mark.asyncio
async def test_metrics_is_404_when_token_is_not_configured_outside_production(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'APP_ENV', 'development')
    monkeypatch.setattr(settings, 'METRICS_TOKEN', '')
    monkeypatch.setattr(main_module, 'SessionLocal', lambda: _ForbiddenDbAccess())

    response = await main_module.metrics(_request())

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_metrics_is_404_for_wrong_token_independent_of_app_env(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'APP_ENV', 'development')
    monkeypatch.setattr(settings, 'METRICS_TOKEN', 'expected-metrics-token')
    monkeypatch.setattr(main_module, 'SessionLocal', lambda: _ForbiddenDbAccess())

    response = await main_module.metrics(_request({'X-Metrics-Token': 'wrong-token'}))

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_metrics_uses_constant_time_comparison_for_wrong_token(monkeypatch) -> None:
    comparisons: list[tuple[str, str]] = []

    def compare_digest(supplied: str, expected: str) -> bool:
        comparisons.append((supplied, expected))
        return False

    monkeypatch.setattr(settings, 'APP_ENV', 'development')
    monkeypatch.setattr(settings, 'METRICS_TOKEN', 'expected-metrics-token')
    monkeypatch.setattr(main_module.hmac, 'compare_digest', compare_digest)
    monkeypatch.setattr(main_module, 'SessionLocal', lambda: _ForbiddenDbAccess())

    response = await main_module.metrics(_request({'X-Metrics-Token': 'wrong-token'}))

    assert response.status_code == 404
    assert comparisons == [('wrong-token', 'expected-metrics-token')]


@pytest.mark.asyncio
async def test_metrics_returns_prometheus_response_for_valid_token(monkeypatch) -> None:
    async def snapshot(_db, _rate: dict) -> dict:
        return {
            'queue_depth': 7,
            'oldest_job_age_seconds': 2.5,
            'unknown_executions': 0,
            'reconciliation_failures_1h': 0,
            'execution_latency_ms_avg_15m': 12.0,
            'execution_reject_rate_15m': 0.0,
            'credential_expiring_7d': 0,
            'watcher_last_event_age_seconds': None,
        }

    async def leverage_snapshot(_redis) -> dict:
        return {}

    monkeypatch.setattr(settings, 'APP_ENV', 'development')
    monkeypatch.setattr(settings, 'METRICS_TOKEN', 'expected-metrics-token')
    monkeypatch.setattr(main_module, 'redis_client', lambda: _Redis())
    monkeypatch.setattr(main_module, 'WeightedRateLimiter', lambda *_args, **_kwargs: _RateLimiter())
    monkeypatch.setattr(main_module, 'SessionLocal', lambda: _DbAccess())
    monkeypatch.setattr(main_module, 'system_snapshot', snapshot)
    monkeypatch.setattr(main_module, 'master_leverage_metric_snapshot', leverage_snapshot)

    response = await main_module.metrics(
        _request({'X-Metrics-Token': 'expected-metrics-token'})
    )

    assert response.status_code == 200
    assert response.media_type == 'text/plain; version=0.0.4'
    assert b'hypercopy_queue_depth 7\n' in response.body
    assert b'hypercopy_redis_used_memory_bytes 123\n' in response.body
