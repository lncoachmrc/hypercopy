from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, require_csrf, scoped_rate_limit, session_claims
from app.core.config import settings
from app.core.security import build_signin_message, create_session_token, hash_ip, normalize_address, verify_wallet_signature
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import AuthNonce, CopyState, Plan, RiskProfile, RiskState, Role, Subscription, User
from app.schemas.auth import ChallengeIn, ChallengeOut, SessionOut, SessionUser, VerifyIn
from app.services.audit import audit
from app.services.entitlement import entitlement

router = APIRouter(prefix='/auth', tags=['auth'])


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
    # Find latest unconsumed nonce for this address in PG, then atomically GETDEL Redis.
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
        db.add(RiskProfile(user_id=user.id)); db.add(RiskState(user_id=user.id))
        if not await db.get(Plan, 'trial'):
            db.add(Plan(slug='trial', name='Trial', limits={'max_multiplier': 1, 'max_notional_per_trade': 1000}))
            await db.flush()
        db.add(Subscription(user_id=user.id, plan_slug='trial', status='trialing', trial_end=datetime.now(UTC)+timedelta(days=settings.TRIAL_DAYS), period_end=datetime.now(UTC)+timedelta(days=settings.TRIAL_DAYS)))
    await audit(db, action='AUTH_LOGIN', actor_id=user.id, subject_id=user.id, ip_hash=hash_ip(request.client.host if request.client else None))
    await db.commit()

    token, csrf = create_session_token(str(user.id), user.auth_wallet, user.role.value)
    secure = settings.APP_ENV != 'development'
    response.set_cookie(settings.SESSION_COOKIE_NAME, token, max_age=settings.SESSION_TTL_SECONDS, httponly=True, secure=secure, samesite='lax', path='/')
    response.set_cookie(settings.CSRF_COOKIE_NAME, csrf, max_age=settings.SESSION_TTL_SECONDS, httponly=False, secure=secure, samesite='lax', path='/')
    ent = await entitlement(db, user)
    return SessionOut(user=SessionUser(id=str(user.id), auth_wallet=user.auth_wallet, role=user.role.value, state=user.state.value, copy_state=user.copy_state.value), entitlements=ent, csrf_token=csrf)


@router.post('/logout', status_code=204, dependencies=[Depends(require_csrf)])
async def logout(response: Response, claims: dict = Depends(session_claims)):
    ttl = max(int(claims.get('exp', 0) - datetime.now(UTC).timestamp()), 1)
    await redis_client().setex(f"session:deny:{claims.get('jti','')}", ttl, '1')
    response.delete_cookie(settings.SESSION_COOKIE_NAME, path='/')
    response.delete_cookie(settings.CSRF_COOKIE_NAME, path='/')


@router.get('/session', response_model=SessionOut)
async def session(user: User = Depends(current_user), claims: dict = Depends(session_claims), db: AsyncSession = Depends(get_db)):
    ent = await entitlement(db, user)
    return SessionOut(user=SessionUser(id=str(user.id), auth_wallet=user.auth_wallet, role=user.role.value, state=user.state.value, copy_state=user.copy_state.value), entitlements=ent, csrf_token=str(claims['csrf']))
