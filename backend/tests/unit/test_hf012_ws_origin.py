from __future__ import annotations

import pytest

from app.api.ws import events
from app.core.config import settings
from app.core.origins import (
    TRAXION_PUBLIC_ORIGIN,
    browser_origins,
    is_allowed_browser_origin,
    normalize_browser_origin,
)


STAGING_ORIGIN = 'https://frontend-staging-9498.up.railway.app'


class _RejectBeforeAuthWebSocket:
    def __init__(self, origin: str | None):
        self.headers = {} if origin is None else {'origin': origin}
        self.closed_codes: list[int] = []

    @property
    def cookies(self):
        raise AssertionError('origin rejection must happen before cookie/session access')

    async def close(self, code: int):
        self.closed_codes.append(code)


class _MissingSessionWebSocket:
    def __init__(self, origin: str):
        self.headers = {'origin': origin}
        self.cookies: dict[str, str] = {}
        self.closed_codes: list[int] = []

    async def close(self, code: int):
        self.closed_codes.append(code)


def test_browser_origins_share_staging_and_public_traxion_policy():
    assert browser_origins(STAGING_ORIGIN + '/') == (
        STAGING_ORIGIN,
        TRAXION_PUBLIC_ORIGIN,
    )
    assert is_allowed_browser_origin(STAGING_ORIGIN, STAGING_ORIGIN)
    assert is_allowed_browser_origin(TRAXION_PUBLIC_ORIGIN, STAGING_ORIGIN)


@pytest.mark.parametrize(
    'origin',
    [
        None,
        'null',
        'https://frontend-staging-9498.up.railway.app.evil.example',
        'https://traxion.lucianonovello.com.evil.example',
        'http://traxion.lucianonovello.com',
        'https://traxion.lucianonovello.com/path',
        'https://user@traxion.lucianonovello.com',
    ],
)
def test_browser_origin_allowlist_rejects_missing_or_non_exact_origins(origin: str | None):
    assert not is_allowed_browser_origin(origin, STAGING_ORIGIN)


def test_origin_normalization_is_exact_and_handles_default_ports():
    assert normalize_browser_origin(' HTTPS://Example.COM:443/ ') == 'https://example.com'
    assert normalize_browser_origin('http://localhost:80') == 'http://localhost'
    assert normalize_browser_origin('https://example.com:8443') == 'https://example.com:8443'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'origin',
    [
        None,
        'null',
        'https://frontend-staging-9498.up.railway.app.evil.example',
        'https://traxion.lucianonovello.com.evil.example',
    ],
)
async def test_ws_events_rejects_untrusted_origin_before_auth(origin: str | None):
    ws = _RejectBeforeAuthWebSocket(origin)

    await events(ws)  # type: ignore[arg-type]

    assert ws.closed_codes == [4403]


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', [STAGING_ORIGIN, TRAXION_PUBLIC_ORIGIN])
async def test_ws_events_allowed_origin_preserves_session_auth_guard(origin: str, monkeypatch):
    monkeypatch.setattr(settings, 'PUBLIC_APP_URL', STAGING_ORIGIN)
    ws = _MissingSessionWebSocket(origin)

    await events(ws)  # type: ignore[arg-type]

    assert ws.closed_codes == [4401]
