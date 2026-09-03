import inspect

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


def test_metrics_token_comparison_is_constant_time() -> None:
    source = inspect.getsource(main_module.metrics)
    assert 'hmac.compare_digest' in source
