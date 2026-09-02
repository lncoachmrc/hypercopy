from __future__ import annotations

import hmac
import time
import uuid
from collections.abc import Callable
from ipaddress import ip_address, ip_network

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decode_session_token, normalize_address
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import Role, User


# Railway reaches the API through carrier-grade/private service networking.
# Only requests whose immediate peer is inside these observed Railway ranges may
# influence client attribution through forwarding headers.
_TRUSTED_PROXY_NETWORKS = (
    ip_network('10.0.0.0/8'),
    ip_network('100.64.0.0/10'),
)
_AUTH_WALLET_LIMITS = {
    '/api/v1/auth/challenge': ('auth-challenge-wallet', 10, 300),
    '/api/v1/auth/verify': ('auth-verify-wallet', 20, 300),
}


def _normalized_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ip_address(value.strip()))
    except ValueError:
        return None


def _trusted_proxy(value: str | None) -> bool:
    normalized = _normalized_ip(value)
    if not normalized:
        return False
    parsed = ip_address(normalized)
    return any(parsed in network for network in _TRUSTED_PROXY_NETWORKS)


def client_ip(request: Request) -> str | None:
    """Resolve the client IP without trusting attacker-controlled leftmost XFF.

    Forwarding headers are considered only when the immediate peer is a known
    Railway private/proxy address. Railway's X-Real-IP is preferred when valid;
    otherwise the X-Forwarded-For chain is walked from right to left and trusted
    proxy hops are discarded until the first untrusted address is found.
    """
    peer = _normalized_ip(request.client.host if request.client else None)
    if not peer:
        return None
    if not _trusted_proxy(peer):
        return peer

    real_ip = _normalized_ip(request.headers.get('x-real-ip'))
    if real_ip:
        return real_ip

    forwarded_for = request.headers.get('x-forwarded-for', '')
    for candidate in reversed(forwarded_for.split(',')):
        normalized = _normalized_ip(candidate)
        if not normalized or _trusted_proxy(normalized):
            continue
        return normalized
    return peer


def normalize_request_client(request: Request) -> None:
    """Expose the resolved client IP to downstream audit/auth code."""
    resolved = client_ip(request)
    if not resolved or not request.client:
        return
    request.scope['client'] = (resolved, request.client.port)


async def session_claims(request: Request) -> dict:
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(401, 'Authentication required')
    try:
        claims = decode_session_token(token)
    except Exception as exc:
        raise HTTPException(401, 'Invalid or expired session') from exc
    redis = redis_client()
    if await redis.exists(f"session:deny:{claims.get('jti','')}"):
        raise HTTPException(401, 'Session revoked')
    session_id = str(claims.get('sid') or '')
    if session_id and await redis.exists(f'session:refresh-deny:{session_id}'):
        raise HTTPException(401, 'Session revoked')
    return claims


async def current_user(claims: dict = Depends(session_claims), db: AsyncSession = Depends(get_db)) -> User:
    user = await db.get(User, uuid.UUID(claims['sub']))
    if not user:
        raise HTTPException(401, 'User no longer exists')
    return user


async def require_csrf(
    request: Request,
    claims: dict = Depends(session_claims),
    x_csrf_token: str | None = Header(default=None, alias='X-CSRF-Token'),
    x_requested_with: str | None = Header(default=None, alias='X-Requested-With'),
) -> None:
    if request.method in {'GET', 'HEAD', 'OPTIONS'}:
        return
    cookie = request.cookies.get(settings.CSRF_COOKIE_NAME, '')
    expected = str(claims.get('csrf', ''))
    if x_requested_with != 'HyperCopy' or not cookie or not x_csrf_token:
        raise HTTPException(403, 'CSRF protection failed')
    if not hmac.compare_digest(cookie, x_csrf_token) or not hmac.compare_digest(cookie, expected):
        raise HTTPException(403, 'CSRF protection failed')


def require_role(*roles: Role) -> Callable:
    async def _dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, 'Insufficient role')
        return user
    return _dep


async def _increment_limit(redis, key: str, limit: int, ttl_seconds: int) -> None:
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, ttl_seconds)
    if count > limit:
        raise HTTPException(429, 'Rate limit exceeded')


async def scoped_rate_limit(
    request: Request,
    scope: str,
    limit: int,
    window_seconds: int,
    *,
    identifier: str | None = None,
) -> None:
    """Redis fixed-window guard keyed by trusted client IP or explicit subject."""
    redis = redis_client()
    subject = identifier or client_ip(request) or 'unknown'
    window = int(time.time()) // window_seconds
    key = f'api:rl:{scope}:{subject}:{window}'
    await _increment_limit(redis, key, limit, window_seconds + 2)


async def _auth_wallet_rate_limit(request: Request) -> None:
    policy = _AUTH_WALLET_LIMITS.get(request.url.path)
    if request.method != 'POST' or not policy:
        return
    try:
        payload = await request.json()
        address = normalize_address(str(payload.get('address') or ''))
    except Exception:
        return
    scope, limit, window_seconds = policy
    await scoped_rate_limit(
        request,
        scope,
        limit,
        window_seconds,
        identifier=address,
    )


async def _session_rate_limit(request: Request, redis) -> None:
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        return
    try:
        claims = decode_session_token(token)
    except Exception:
        return
    session_id = str(claims.get('sid') or '')
    if not session_id:
        return
    window = int(time.time()) // 60
    key = f'api:rl:session:{session_id}:{window}'
    await _increment_limit(redis, key, settings.API_RATE_LIMIT_PER_MIN, 62)


async def api_rate_limit(request: Request) -> None:
    redis = redis_client()
    ip = client_ip(request) or 'unknown'
    await _increment_limit(redis, f'api:rl:{ip}', settings.API_RATE_LIMIT_PER_MIN, 60)
    await _auth_wallet_rate_limit(request)
    await _session_rate_limit(request, redis)
