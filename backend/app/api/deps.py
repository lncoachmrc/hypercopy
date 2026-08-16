from __future__ import annotations

import hmac
import uuid
import time
from collections.abc import Callable

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decode_session_token
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import Role, User


async def session_claims(request: Request) -> dict:
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(401, 'Authentication required')
    try:
        claims = decode_session_token(token)
    except Exception as exc:
        raise HTTPException(401, 'Invalid or expired session') from exc
    if await redis_client().exists(f"session:deny:{claims.get('jti','')}"):
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


async def api_rate_limit(request: Request) -> None:
    redis = redis_client()
    ip = request.client.host if request.client else 'unknown'
    key = f'api:rl:{ip}'
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)
    if count > settings.API_RATE_LIMIT_PER_MIN:
        raise HTTPException(429, 'Rate limit exceeded')


async def scoped_rate_limit(request: Request, scope: str, limit: int, window_seconds: int) -> None:
    """Small Redis-backed fixed-window guard for sensitive public endpoints.

    This is intentionally separate from the global API limit so challenge/verify
    retain their tighter SPEC budgets.
    """
    redis = redis_client()
    ip = request.client.host if request.client else 'unknown'
    window = int(time.time()) // window_seconds
    key = f'api:rl:{scope}:{ip}:{window}'
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds + 2)
    if count > limit:
        raise HTTPException(429, 'Rate limit exceeded')
