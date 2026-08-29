from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, require_csrf, scoped_rate_limit, session_claims
from app.core.config import settings
from app.core.security import (
    build_signin_message,
    create_refresh_token,
    create_session_token,
    derive_refresh_successor,
    hash_ip,
    hash_refresh_token,
    normalize_address,
    verify_wallet_signature,
)
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import AuthNonce, CopyState, Plan, RiskProfile, RiskState, Role, Subscription, User
from app.schemas.auth import ChallengeIn, ChallengeOut, SessionOut, SessionUser, VerifyIn
from app.services.audit import audit
from app.services.entitlement import entitlement
from app.services.networking import set_user_network

router = APIRouter(prefix='/auth', tags=['auth'])
_REFRESH_PREFIX = 'session:refresh:'
_REFRESH_DENY_PREFIX = 'session:refresh-deny:'


def _secure_cookie() -> bool:
    return settings.APP_ENV != 'development'


def _refresh_key(token: str) -> str:
    return f'{_REFRESH_PREFIX}{hash_refresh_token(token)}'


def _refresh_deny_key(session_id: str) -> str:
    return f'{_REFRESH_DENY_PREFIX}{session_id}'


def _refresh_payload(user: User, *, absolute_exp: int, session_id: str) -> dict:
    return {
        'sub': str(user.id),
        'wallet': user.auth_wallet,
        'absolute_exp': absolute_exp,
        'session_id': session_id,
    }


def _session_out(user: User, entitlements: dict, csrf: str) -> SessionOut:
    return SessionOut(
        user=SessionUser(
            id=str(user.id),
            auth_wallet=user.auth_wallet,
            role=user.role.value,
            state=user.state.value,
            copy_state=user.copy_state.value,
        ),
        entitlements=entitlements,
        csrf_token=csrf,
    )


def _set_access_cookies(response: Response, token: str, csrf: str) -> None:
    secure = _secure_cookie()
    response.set_cookie(
        settings.SESSION_COOKIE_NAME,
        token,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite='lax',
        path='/',
    )
    response.set_cookie(
        settings.CSRF_COOKIE_NAME,
        csrf,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=False,
        secure=secure,
        samesite='lax',
        path='/',
    )


def _set_refresh_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        settings.SESSION_REFRESH_COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=_secure_cookie(),
        samesite='lax',
        path='/',
    )


def _clear_session_cookies(response: Response) -> None:
    secure = _secure_cookie()
    response.delete_cookie(settings.SESSION_COOKIE_NAME, path='/', secure=secure, httponly=True, samesite='lax')
    response.delete_cookie(settings.CSRF_COOKIE_NAME, path='/', secure=secure, httponly=False, samesite='lax')
    response.delete_cookie(settings.SESSION_REFRESH_COOKIE_NAME, path='/', secure=secure, httponly=True, samesite='lax')


async def _store_refresh_token(token: str, user: User, *, absolute_exp: int, session_id: str) -> int:
    now_ts = int(datetime.now(UTC).timestamp())
    ttl = absolute_exp - now_ts
    if ttl <= 0:
        raise HTTPException(401, 'Refresh session expired')
    redis = redis_client()
    if await redis.exists(_refresh_deny_key(session_id)):
        raise HTTPException(401, 'Refresh session revoked')
    await redis.setex(
        _refresh_key(token),
        ttl,
        json.dumps(_refresh_payload(user, absolute_exp=absolute_exp, session_id=session_id)),
    )
    return ttl


async def _issue_refresh_token(user: User, *, absolute_exp: int, session_id: str) -> tuple[str, int]:
    token = create_refresh_token()
    ttl = await _store_refresh_token(token, user, absolute_exp=absolute_exp, session_id=session_id)
    return token, ttl


async def _rotate_refresh_token(
    current_token: str,
    stored_payload: dict,
    user: User,
    *,
    absolute_exp: int,
    session_id: str,
) -> tuple[str, int]:
    """Rotate without making a lost response force a wallet signature.

    The successor is deterministic under SESSION_SECRET, so the old credential
    can reproduce exactly the same successor during a bounded grace interval.
    Redis still stores only token digests as keys, never successor plaintext.
    """
    now_ts = int(datetime.now(UTC).timestamp())
    ttl = absolute_exp - now_ts
    if ttl <= 0:
        raise HTTPException(401, 'Refresh session expired')

    redis = redis_client()
    if await redis.exists(_refresh_deny_key(session_id)):
        raise HTTPException(401, 'Refresh session revoked')

    successor = derive_refresh_successor(current_token)
    successor_hash = hash_refresh_token(successor)
    rotated_to = str(stored_payload.get('rotated_to') or '')

    if rotated_to:
        if rotated_to != successor_hash:
            await redis.delete(_refresh_key(current_token))
            raise HTTPException(401, 'Invalid refresh rotation state')
        # A prior response may have been lost. The old handle is intentionally
        # idempotent only until its original rotation_grace_exp; never extend it.
        grace_exp = int(stored_payload.get('rotation_grace_exp') or 0)
        if grace_exp <= now_ts:
            await redis.delete(_refresh_key(current_token))
            raise HTTPException(401, 'Refresh rotation grace expired')
        if not await redis.exists(_refresh_key(successor)):
            await _store_refresh_token(successor, user, absolute_exp=absolute_exp, session_id=session_id)
        return successor, ttl

    # Store the successor first. If this write fails, the old credential remains
    # active. If a later step or HTTP delivery fails, the old credential becomes
    # a short-lived idempotency handle that can recover the same successor.
    await _store_refresh_token(successor, user, absolute_exp=absolute_exp, session_id=session_id)
    grace_seconds = min(settings.SESSION_REFRESH_GRACE_SECONDS, ttl)
    grace_exp = now_ts + grace_seconds
    grace_payload = {
        **_refresh_payload(user, absolute_exp=absolute_exp, session_id=session_id),
        'rotated_to': successor_hash,
        'rotation_grace_exp': grace_exp,
    }
    await redis.setex(_refresh_key(current_token), grace_seconds, json.dumps(grace_payload))
    return successor, ttl


@router.post('/challenge', response_model=ChallengeOut)
async def challenge(body: ChallengeIn, request: Request, db: AsyncSession = Depends(get_db)):
    await scoped_rate_limit(request, 'auth-challenge', 10, 300)
    try:
        address = normalize_address(body.address)
    except Exception as exc:
        raise HTTPException(422, 'Invalid wallet address') from exc
    nonce = secrets.token_hex(12)
    now = datetime.now(UTC); expires = now + timedelta(seconds=settings.AUTH_NONCE_TTL_SECONDS)
    message = build_signin_message(address, nonce, now, expires)
    await redis_client().setex(f'auth:nonce:{nonce}', settings.AUTH_NONCE_TTL_SECONDS, f'{address}\n{message}')
    db.add(AuthNonce(nonce=nonce, address=address, expires_at=expires))
    await audit(db, action='AUTH_CHALLENGE', ip_hash=hash_ip(request.client.host if request.client else None), after={'wallet': address[:8] + '…'})
    await db.commit()
    return ChallengeOut(message=message, expires_at=expires)


@router.post('/verify', response_model=SessionOut)
async def verify(body: VerifyIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    await scoped_rate_limit(request, 'auth-verify', 20, 300)
    address = normalize_address(body.address)
    nonce_row = (await db.execute(select(AuthNonce).where(AuthNonce.address == address, AuthNonce.consumed_at.is_(None), AuthNonce.expires_at > datetime.now(UTC)).order_by(AuthNonce.created_at.desc()).limit(1))).scalar_one_or_none()
    if not nonce_row:
        raise HTTPException(401, 'Challenge expired or already used')
    stored = await redis_client().getdel(f'auth:nonce:{nonce_row.nonce}')
    if not stored:
        raise HTTPException(401, 'Challenge expired or already used')
    stored_address, message = stored.split('\n', 1)
    if stored_address != address or not verify_wallet_signature(address, message, body.signature):
        raise HTTPException(401, 'Invalid wallet signature')
    nonce_row.consumed_at = datetime.now(UTC)

    user = (await db.execute(select(User).where(User.auth_wallet == address))).scalar_one_or_none()
    if not user:
        role = Role.SUPERADMIN if address in settings.superadmin_addresses else Role.ADMIN if address in settings.admin_addresses else Role.USER
        user = User(auth_wallet=address, role=role, copy_state=CopyState.SHADOW, shadow_started_at=datetime.now(UTC))
        db.add(user); await db.flush()
        # Do not inherit the historical schema default (TESTNET). New accounts
        # start on the deployment's configured follower network; the current
        # operational fallback is MAINNET while local/test can override it.
        await set_user_network(db, user.id, settings.follower_network)
        db.add(RiskProfile(user_id=user.id)); db.add(RiskState(user_id=user.id))
        if not await db.get(Plan, 'trial'):
            db.add(Plan(slug='trial', name='Trial', limits={
                'max_multiplier': 1,
                'max_notional_per_trade': 500,
                'max_positions': 3,
                'max_equity_usd': 1000,
            }))
            await db.flush()
        trial_end = datetime.now(UTC) + timedelta(days=settings.TRIAL_DAYS)
        db.add(Subscription(user_id=user.id, plan_slug='trial', status='trialing', trial_end=trial_end, period_end=trial_end))
    await audit(db, action='AUTH_LOGIN', actor_id=user.id, subject_id=user.id, ip_hash=hash_ip(request.client.host if request.client else None))
    await db.commit()

    absolute_exp = int(datetime.now(UTC).timestamp()) + settings.SESSION_REFRESH_TTL_SECONDS
    session_id = secrets.token_urlsafe(18)
    token, csrf = create_session_token(
        str(user.id),
        user.auth_wallet,
        user.role.value,
        session_id=session_id,
        session_absolute_exp=absolute_exp,
    )
    refresh_token, refresh_ttl = await _issue_refresh_token(
        user,
        absolute_exp=absolute_exp,
        session_id=session_id,
    )
    _set_access_cookies(response, token, csrf)
    _set_refresh_cookie(response, refresh_token, refresh_ttl)
    ent = await entitlement(db, user)
    return _session_out(user, ent, csrf)


@router.post('/refresh', response_model=SessionOut)
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    await scoped_rate_limit(request, 'auth-refresh', 60, 300)
    if request.headers.get('x-requested-with') != 'HyperCopy':
        raise HTTPException(403, 'Refresh protection failed')

    refresh_token = request.cookies.get(settings.SESSION_REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise HTTPException(401, 'Refresh session required')

    redis = redis_client()
    key = _refresh_key(refresh_token)
    stored = await redis.get(key)
    if not stored:
        raise HTTPException(401, 'Refresh session expired')

    try:
        payload = json.loads(stored)
        user_id = uuid.UUID(str(payload['sub']))
        absolute_exp = int(payload['absolute_exp'])
        session_id = str(payload['session_id'])
        wallet = normalize_address(str(payload['wallet']))
    except Exception as exc:
        await redis.delete(key)
        raise HTTPException(401, 'Invalid refresh session') from exc

    now_ts = int(datetime.now(UTC).timestamp())
    if absolute_exp <= now_ts:
        await redis.delete(key)
        raise HTTPException(401, 'Refresh session expired')
    if await redis.exists(_refresh_deny_key(session_id)):
        await redis.delete(key)
        raise HTTPException(401, 'Refresh session revoked')

    user = await db.get(User, user_id)
    if not user or user.auth_wallet != wallet:
        await redis.delete(key)
        raise HTTPException(401, 'Refresh session no longer valid')

    new_refresh, refresh_ttl = await _rotate_refresh_token(
        refresh_token,
        payload,
        user,
        absolute_exp=absolute_exp,
        session_id=session_id,
    )
    token, csrf = create_session_token(
        str(user.id),
        user.auth_wallet,
        user.role.value,
        session_id=session_id,
        session_absolute_exp=absolute_exp,
    )
    _set_access_cookies(response, token, csrf)
    _set_refresh_cookie(response, new_refresh, refresh_ttl)
    ent = await entitlement(db, user)
    return _session_out(user, ent, csrf)


@router.post('/logout', status_code=204, dependencies=[Depends(require_csrf)])
async def logout(request: Request, response: Response, claims: dict = Depends(session_claims)):
    now_ts = int(datetime.now(UTC).timestamp())
    access_ttl = max(int(claims.get('exp', 0)) - now_ts, 1)
    redis = redis_client()
    await redis.setex(f"session:deny:{claims.get('jti','')}", access_ttl, '1')

    session_id = str(claims.get('sid') or '')
    session_exp = int(claims.get('session_exp') or 0)
    if session_id:
        family_ttl = max(session_exp - now_ts, 1) if session_exp else settings.SESSION_REFRESH_TTL_SECONDS
        await redis.setex(_refresh_deny_key(session_id), family_ttl, '1')

    refresh_token = request.cookies.get(settings.SESSION_REFRESH_COOKIE_NAME)
    if refresh_token:
        await redis.delete(_refresh_key(refresh_token))
    _clear_session_cookies(response)


@router.get('/session', response_model=SessionOut)
async def session(user: User = Depends(current_user), claims: dict = Depends(session_claims), db: AsyncSession = Depends(get_db)):
    ent = await entitlement(db, user)
    return _session_out(user, ent, str(claims['csrf']))