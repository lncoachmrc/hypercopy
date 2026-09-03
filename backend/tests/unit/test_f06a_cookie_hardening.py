from __future__ import annotations

from http.cookies import Morsel, SimpleCookie

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from app.api import auth, deps, ws as ws_api
from app.core.config import settings


def _request(
    *,
    path: str,
    method: str = 'GET',
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers: list[tuple[bytes, bytes]] = []
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode('ascii'), value.encode('ascii')))
    if cookies:
        cookie_value = '; '.join(f'{key}={value}' for key, value in cookies.items())
        raw_headers.append((b'cookie', cookie_value.encode('ascii')))
    return Request(
        {
            'type': 'http',
            'http_version': '1.1',
            'method': method,
            'scheme': 'https',
            'path': path,
            'raw_path': path.encode('ascii'),
            'query_string': b'',
            'headers': raw_headers,
            'client': ('127.0.0.1', 12345),
            'server': ('traxion.test', 443),
        }
    )


def _cookie(response: Response, name: str) -> Morsel[str]:
    for header in response.headers.getlist('set-cookie'):
        parsed = SimpleCookie()
        parsed.load(header)
        if name in parsed:
            return parsed[name]
    raise AssertionError(f'cookie {name!r} not found')


class _FakeRedis:
    def __init__(self, *, get_value=None):
        self.get_value = get_value
        self.deleted: list[str] = []
        self.incremented: list[str] = []

    async def exists(self, _key: str) -> bool:
        return False

    async def get(self, _key: str):
        return self.get_value

    async def delete(self, key: str) -> None:
        self.deleted.append(key)

    async def setex(self, _key: str, _ttl: int, _value: str) -> None:
        return None

    async def incr(self, key: str) -> int:
        self.incremented.append(key)
        return 1

    async def expire(self, _key: str, _ttl: int) -> None:
        return None


def test_production_access_and_csrf_cookies_use_exact_host_contract(monkeypatch):
    monkeypatch.setattr(auth, '_secure_cookie', lambda: True)
    response = Response()

    auth._set_access_cookies(response, 'session-token', 'csrf-token')

    session = _cookie(response, f'__Host-{settings.SESSION_COOKIE_NAME}')
    csrf = _cookie(response, f'__Host-{settings.CSRF_COOKIE_NAME}')

    assert session.value == 'session-token'
    assert session['path'] == '/'
    assert session['domain'] == ''
    assert bool(session['secure'])
    assert bool(session['httponly'])

    assert csrf.value == 'csrf-token'
    assert csrf['path'] == '/'
    assert csrf['domain'] == ''
    assert bool(csrf['secure'])
    assert not bool(csrf['httponly'])


def test_production_refresh_cookie_uses_exact_secure_contract(monkeypatch):
    monkeypatch.setattr(auth, '_secure_cookie', lambda: True)
    response = Response()

    auth._set_refresh_cookie(response, 'refresh-token', 300)

    refresh = _cookie(response, f'__Secure-{settings.SESSION_REFRESH_COOKIE_NAME}')
    assert refresh.value == 'refresh-token'
    assert refresh['path'] == '/api/v1/auth/refresh'
    assert refresh['domain'] == ''
    assert bool(refresh['secure'])
    assert bool(refresh['httponly'])


def test_logout_clears_prefixed_cookies_with_exact_matching_paths(monkeypatch):
    monkeypatch.setattr(auth, '_secure_cookie', lambda: True)
    response = Response()

    auth._clear_session_cookies(response)

    session = _cookie(response, f'__Host-{settings.SESSION_COOKIE_NAME}')
    csrf = _cookie(response, f'__Host-{settings.CSRF_COOKIE_NAME}')
    refresh = _cookie(response, f'__Secure-{settings.SESSION_REFRESH_COOKIE_NAME}')

    assert session['path'] == '/'
    assert session['domain'] == ''
    assert csrf['path'] == '/'
    assert csrf['domain'] == ''
    assert refresh['path'] == '/api/v1/auth/refresh'
    assert refresh['domain'] == ''


def test_development_keeps_unprefixed_names_but_refresh_path_is_exact(monkeypatch):
    monkeypatch.setattr(auth, '_secure_cookie', lambda: False)
    access_response = Response()
    refresh_response = Response()

    auth._set_access_cookies(access_response, 'session-token', 'csrf-token')
    auth._set_refresh_cookie(refresh_response, 'refresh-token', 300)

    session = _cookie(access_response, settings.SESSION_COOKIE_NAME)
    csrf = _cookie(access_response, settings.CSRF_COOKIE_NAME)
    refresh = _cookie(refresh_response, settings.SESSION_REFRESH_COOKIE_NAME)

    assert session['path'] == '/'
    assert csrf['path'] == '/'
    assert refresh['path'] == '/api/v1/auth/refresh'
    assert not bool(session['secure'])
    assert not bool(csrf['secure'])
    assert not bool(refresh['secure'])


@pytest.mark.asyncio
async def test_session_and_csrf_consumers_read_prefixed_production_names(monkeypatch):
    session_name = f'__Host-{settings.SESSION_COOKIE_NAME}'
    csrf_name = f'__Host-{settings.CSRF_COOKIE_NAME}'
    fake_redis = _FakeRedis()
    claims = {'jti': 'jti-1', 'sid': 'sid-1', 'csrf': 'csrf-value', 'sub': '00000000-0000-0000-0000-000000000001'}

    monkeypatch.setattr(deps, 'session_cookie_name', lambda: session_name)
    monkeypatch.setattr(deps, 'csrf_cookie_name', lambda: csrf_name)
    monkeypatch.setattr(deps, 'decode_session_token', lambda token: claims if token == 'session-token' else {})
    monkeypatch.setattr(deps, 'redis_client', lambda: fake_redis)

    request = _request(
        path='/api/v1/account',
        method='POST',
        cookies={session_name: 'session-token', csrf_name: 'csrf-value'},
    )

    resolved = await deps.session_claims(request)
    assert resolved == claims
    await deps.require_csrf(
        request,
        claims,
        x_csrf_token='csrf-value',
        x_requested_with='HyperCopy',
    )


@pytest.mark.asyncio
async def test_session_rate_limit_reads_prefixed_production_session_cookie(monkeypatch):
    session_name = f'__Host-{settings.SESSION_COOKIE_NAME}'
    fake_redis = _FakeRedis()
    monkeypatch.setattr(deps, 'session_cookie_name', lambda: session_name)
    monkeypatch.setattr(deps, 'decode_session_token', lambda token: {'sid': 'sid-rate'} if token == 'session-token' else {})

    request = _request(path='/api/v1/positions', cookies={session_name: 'session-token'})
    await deps._session_rate_limit(request, fake_redis)

    assert any(':sid-rate:' in key for key in fake_redis.incremented)


@pytest.mark.asyncio
async def test_refresh_consumer_reads_prefixed_production_refresh_cookie(monkeypatch):
    refresh_name = f'__Secure-{settings.SESSION_REFRESH_COOKIE_NAME}'
    fake_redis = _FakeRedis(get_value=None)

    async def _no_rate_limit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(auth, 'refresh_cookie_name', lambda: refresh_name)
    monkeypatch.setattr(auth, 'scoped_rate_limit', _no_rate_limit)
    monkeypatch.setattr(auth, 'redis_client', lambda: fake_redis)

    request = _request(
        path='/api/v1/auth/refresh',
        method='POST',
        cookies={refresh_name: 'refresh-token'},
        headers={'X-Requested-With': 'HyperCopy'},
    )

    with pytest.raises(HTTPException) as exc_info:
        await auth.refresh(request, Response(), None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == 'Refresh session expired'


@pytest.mark.asyncio
async def test_logout_consumer_recognizes_prefixed_refresh_when_explicitly_supplied(monkeypatch):
    refresh_name = f'__Secure-{settings.SESSION_REFRESH_COOKIE_NAME}'
    fake_redis = _FakeRedis()
    monkeypatch.setattr(auth, 'refresh_cookie_name', lambda: refresh_name)
    monkeypatch.setattr(auth, 'redis_client', lambda: fake_redis)

    request = _request(
        path='/api/v1/auth/logout',
        method='POST',
        cookies={refresh_name: 'refresh-token'},
    )
    claims = {'jti': 'jti-logout', 'exp': 9999999999, 'sid': '', 'session_exp': 0}

    await auth.logout(request, Response(), claims)
    assert auth._refresh_key('refresh-token') in fake_redis.deleted


@pytest.mark.asyncio
async def test_websocket_consumer_reads_prefixed_production_session_cookie(monkeypatch):
    session_name = f'__Host-{settings.SESSION_COOKIE_NAME}'
    observed: list[str] = []

    class _FakeWebSocket:
        headers = {'origin': 'https://app.traxion.test'}
        cookies = {session_name: 'session-token'}

        def __init__(self):
            self.closed: list[int] = []

        async def close(self, code: int):
            self.closed.append(code)

    def _decode(token: str):
        observed.append(token)
        raise ValueError('stop after cookie lookup')

    monkeypatch.setattr(ws_api, 'session_cookie_name', lambda: session_name)
    monkeypatch.setattr(ws_api, 'is_allowed_browser_origin', lambda *_args: True)
    monkeypatch.setattr(ws_api, 'decode_session_token', _decode)

    websocket = _FakeWebSocket()
    await ws_api.events(websocket)

    assert observed == ['session-token']
    assert websocket.closed == [4401]
