from fastapi import Response

from app.api import auth
from app.core.config import settings


def _set_cookie_headers(response: Response) -> list[str]:
    return response.headers.getlist('set-cookie')


def test_production_access_and_csrf_cookies_use_host_prefix(monkeypatch):
    monkeypatch.setattr(auth, '_secure_cookie', lambda: True)
    response = Response()

    auth._set_access_cookies(response, 'session-token', 'csrf-token')

    headers = _set_cookie_headers(response)
    assert any(
        header.startswith(f'__Host-{settings.SESSION_COOKIE_NAME}=session-token;')
        and 'Path=/' in header
        and 'Secure' in header
        and 'HttpOnly' in header
        for header in headers
    )
    assert any(
        header.startswith(f'__Host-{settings.CSRF_COOKIE_NAME}=csrf-token;')
        and 'Path=/' in header
        and 'Secure' in header
        and 'HttpOnly' not in header
        for header in headers
    )


def test_production_refresh_cookie_uses_secure_prefix_and_narrow_path(monkeypatch):
    monkeypatch.setattr(auth, '_secure_cookie', lambda: True)
    response = Response()

    auth._set_refresh_cookie(response, 'refresh-token', 300)

    headers = _set_cookie_headers(response)
    assert len(headers) == 1
    assert headers[0].startswith(f'__Secure-{settings.SESSION_REFRESH_COOKIE_NAME}=refresh-token;')
    assert 'Path=/api/v1/auth/refresh' in headers[0]
    assert 'Secure' in headers[0]
    assert 'HttpOnly' in headers[0]


def test_logout_clears_prefixed_cookies_with_matching_paths(monkeypatch):
    monkeypatch.setattr(auth, '_secure_cookie', lambda: True)
    response = Response()

    auth._clear_session_cookies(response)

    headers = _set_cookie_headers(response)
    assert any(
        header.startswith(f'__Host-{settings.SESSION_COOKIE_NAME}=') and 'Path=/' in header
        for header in headers
    )
    assert any(
        header.startswith(f'__Host-{settings.CSRF_COOKIE_NAME}=') and 'Path=/' in header
        for header in headers
    )
    assert any(
        header.startswith(f'__Secure-{settings.SESSION_REFRESH_COOKIE_NAME}=')
        and 'Path=/api/v1/auth/refresh' in header
        for header in headers
    )


def test_development_keeps_unprefixed_names_but_refresh_path_is_narrow(monkeypatch):
    monkeypatch.setattr(auth, '_secure_cookie', lambda: False)
    access_response = Response()
    refresh_response = Response()

    auth._set_access_cookies(access_response, 'session-token', 'csrf-token')
    auth._set_refresh_cookie(refresh_response, 'refresh-token', 300)

    access_headers = _set_cookie_headers(access_response)
    refresh_headers = _set_cookie_headers(refresh_response)
    assert any(header.startswith(f'{settings.SESSION_COOKIE_NAME}=session-token;') for header in access_headers)
    assert any(header.startswith(f'{settings.CSRF_COOKIE_NAME}=csrf-token;') for header in access_headers)
    assert refresh_headers[0].startswith(f'{settings.SESSION_REFRESH_COOKIE_NAME}=refresh-token;')
    assert 'Path=/api/v1/auth/refresh' in refresh_headers[0]
    assert 'Secure' not in refresh_headers[0]
