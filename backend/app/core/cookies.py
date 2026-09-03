from __future__ import annotations

from app.core.config import settings

REFRESH_COOKIE_PATH = '/api/v1/auth/refresh'


def cookie_secure() -> bool:
    return settings.APP_ENV != 'development'


def _prefixed_cookie_name(name: str, prefix: str, *, secure: bool) -> str:
    if not secure:
        return name
    if name.startswith(prefix):
        return name
    return f'{prefix}{name}'


def session_cookie_name(*, secure: bool | None = None) -> str:
    resolved_secure = cookie_secure() if secure is None else secure
    return _prefixed_cookie_name(settings.SESSION_COOKIE_NAME, '__Host-', secure=resolved_secure)


def csrf_cookie_name(*, secure: bool | None = None) -> str:
    resolved_secure = cookie_secure() if secure is None else secure
    return _prefixed_cookie_name(settings.CSRF_COOKIE_NAME, '__Host-', secure=resolved_secure)


def refresh_cookie_name(*, secure: bool | None = None) -> str:
    resolved_secure = cookie_secure() if secure is None else secure
    return _prefixed_cookie_name(settings.SESSION_REFRESH_COOKIE_NAME, '__Secure-', secure=resolved_secure)
