from __future__ import annotations

import pytest
from starlette.requests import Request

from app.api import deps
from app.core.config import settings


class _FakeRedis:
    def __init__(self) -> None:
        self.incr_keys: list[str] = []
        self.expired: list[tuple[str, int]] = []

    async def incr(self, key: str) -> int:
        self.incr_keys.append(key)
        return 1

    async def expire(self, key: str, ttl: int) -> bool:
        self.expired.append((key, ttl))
        return True


def _request(
    *,
    client_host: str,
    forwarded_for: str | None = None,
    session_cookie: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    if session_cookie is not None:
        cookie = f"{settings.SESSION_COOKIE_NAME}={session_cookie}"
        headers.append((b"cookie", cookie.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/test",
            "raw_path": b"/api/v1/test",
            "query_string": b"",
            "headers": headers,
            "client": (client_host, 50000),
            "server": ("testserver", 443),
        }
    )


@pytest.mark.asyncio
async def test_scoped_rate_limit_uses_rightmost_untrusted_forwarded_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    monkeypatch.setattr(deps, "redis_client", lambda: redis)
    request = _request(
        client_host="10.208.202.255",
        forwarded_for="198.51.100.77, 203.0.113.9, 100.64.0.16",
    )

    await deps.scoped_rate_limit(request, "auth-challenge", 10, 300)

    assert any(":203.0.113.9:" in key for key in redis.incr_keys)
    assert all("198.51.100.77" not in key for key in redis.incr_keys)
    assert all("10.208.202.255" not in key for key in redis.incr_keys)


@pytest.mark.asyncio
async def test_scoped_rate_limit_ignores_forwarded_chain_from_untrusted_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    monkeypatch.setattr(deps, "redis_client", lambda: redis)
    request = _request(
        client_host="203.0.113.44",
        forwarded_for="198.51.100.99",
    )

    await deps.scoped_rate_limit(request, "auth-challenge", 10, 300)

    assert any(":203.0.113.44:" in key for key in redis.incr_keys)
    assert all("198.51.100.99" not in key for key in redis.incr_keys)


@pytest.mark.asyncio
async def test_scoped_rate_limit_supports_wallet_identifier_independent_of_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    monkeypatch.setattr(deps, "redis_client", lambda: redis)
    request = _request(client_host="203.0.113.44")

    await deps.scoped_rate_limit(
        request,
        "auth-challenge-wallet",
        10,
        300,
        identifier="0x1111111111111111111111111111111111111111",
    )

    assert any(
        "auth-challenge-wallet:0x1111111111111111111111111111111111111111:" in key
        for key in redis.incr_keys
    )


@pytest.mark.asyncio
async def test_global_api_rate_limit_adds_signed_session_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    monkeypatch.setattr(deps, "redis_client", lambda: redis)
    monkeypatch.setattr(deps, "decode_session_token", lambda token: {"sid": "session-123"})
    request = _request(client_host="203.0.113.44", session_cookie="signed-session-token")

    await deps.api_rate_limit(request)

    assert any("api:rl:session:session-123" in key for key in redis.incr_keys)
