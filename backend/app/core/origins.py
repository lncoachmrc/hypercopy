from __future__ import annotations

from urllib.parse import urlsplit

TRAXION_PUBLIC_ORIGIN = 'https://traxion.lucianonovello.com'


def normalize_browser_origin(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
            return None
        port = parsed.port
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    if port in {80 if scheme == 'http' else 443}:
        port = None
    return f'{scheme}://{host}{f":{port}" if port is not None else ""}'


def browser_origins(public_app_url: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in (public_app_url, TRAXION_PUBLIC_ORIGIN):
        origin = normalize_browser_origin(value)
        if origin is not None and origin not in normalized:
            normalized.append(origin)
    return tuple(normalized)


def is_allowed_browser_origin(origin: str | None, public_app_url: str) -> bool:
    normalized = normalize_browser_origin(origin)
    return normalized is not None and normalized in browser_origins(public_app_url)
