from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Budget, WeightedRateLimiter
from app.api.deps import current_user, require_csrf
from app.core.config import settings
from app.core.crypto import crypto
from app.core.security import hash_ip, normalize_address
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import CopyJob, CopyState, CredentialStatus, Execution, PositionLedger, RiskHalt, RiskProfile, RiskState, SigningCredential, TradingAccount, User
from app.schemas.trading import ClosePositionsIn
from app.schemas.user import RiskProfileIn, TradingAccountIn
from app.services.audit import audit
from app.services.entitlement import entitlement
from app.services.metrics import dashboard_for_user
from app.services.reconcile import master_snapshot, reconcile_user

router = APIRouter(tags=['user'])


def _hl() -> HyperliquidAdapter:
    return HyperliquidAdapter(WeightedRateLimiter(redis_client(), Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN)))


async def _serialize_user(db: AsyncSession, user: User) -> dict:
    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    cred = None
    if account:
        cred = (await db.execute(select(SigningCredential).where(SigningCredential.trading_account_id == account.id))).scalar_one_or_none()
    rs = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
    return {
        'id': str(user.id), 'auth_wallet': user.auth_wallet, 'role': user.role.value, 'state': user.state.value,
        'copy_state': user.copy_state.value, 'manual_trade_policy': user.manual_trade_policy.value,
        'display_name': user.display_name, 'email': user.email, 'shadow_started_at': user.shadow_started_at,
        'risk_state': rs.state.value if rs else RiskHalt.NORMAL.value,
        'trading_account': None if not account else {'account_address': account.account_address, 'agent_address': account.agent_address, 'agent_name': account.agent_name, 'verified_at': account.verified_at, 'credential_status': cred.status.value if cred else None, 'expires_at': cred.expires_at if cred else None},
    }


@router.get('/me')
async def me(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _serialize_user(db, user)


@router.get('/dashboard')
async def dashboard(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    data = await dashboard_for_user(db, user.id); data['user'] = await _serialize_user(db, user); data['entitlements'] = await entitlement(db, user); return data


@router.get('/positions')
async def positions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(PositionLedger).where(PositionLedger.user_id == user.id).order_by(PositionLedger.asset))).scalars().all()
    return [{'asset': r.asset, 'current_size': r.size, 'target_size': r.target_size, 'delta': r.target_size-r.size, 'managed': r.managed, 'exchange_verified_at': r.exchange_verified_at} for r in rows]


@router.get('/executions')
async def executions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db), limit: int = 50, offset: int = 0, state: str | None = None, asset: str | None = None):
    q = select(Execution).where(Execution.user_id == user.id)
    if state: q = q.where(Execution.state == state)
    if asset: q = q.where(Execution.asset == asset)
    rows = (await db.execute(q.order_by(Execution.created_at.desc()).offset(offset).limit(min(limit, 200)))).scalars().all()
    return [{'id': str(r.id), 'asset': r.asset, 'state': r.state.value, 'is_buy': r.is_buy, 'requested_size': r.requested_size, 'filled_size': r.filled_size, 'avg_price': r.avg_price, 'reduce_only': r.reduce_only, 'reject_reason': r.reject_reason, 'cloid': r.cloid, 'created_at': r.created_at} for r in rows]


@router.post('/trading-account', dependencies=[Depends(require_csrf)])
async def link_trading_account(body: TradingAccountIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    # Product invariant: the Web3 wallet that authenticated the user is the
    # Hyperliquid follower account.  Users cannot redirect execution to a
    # different account by supplying another address in this request.
    account_address = normalize_address(user.auth_wallet)
    master_address = normalize_address(settings.HYPERLIQUID_MASTER_ADDRESS) if settings.HYPERLIQUID_MASTER_ADDRESS else ''
    if master_address and account_address == master_address:
        raise HTTPException(422, 'The master Hyperliquid account cannot also be linked as a follower account')

    expected_agent_address = None
    if body.agent_address:
        try:
            expected_agent_address = normalize_address(body.agent_address)
        except Exception as exc:
            raise HTTPException(422, 'Invalid API Wallet address') from exc

    try:
        verification = await _hl().verify_agent(
            account_address,
            body.agent_private_key,
            expected_agent_address=expected_agent_address,
        )
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc

    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if account and account.account_address.lower() != account_address.lower():
        open_managed = (await db.execute(select(PositionLedger.id).where(
            PositionLedger.user_id == user.id, PositionLedger.managed.is_(True), PositionLedger.size != 0
        ).limit(1))).scalar_one_or_none()
        if open_managed:
            raise HTTPException(409, 'Close all HyperCopy-managed positions before changing the Hyperliquid account')
    if not account:
        account = TradingAccount(user_id=user.id, account_address=account_address, agent_address=verification.agent_address, agent_name=verification.name)
        db.add(account); await db.flush()
    else:
        account.account_address, account.agent_address, account.agent_name, account.verified_at = account_address, verification.agent_address, verification.name, datetime.now(UTC)
        old = (await db.execute(select(SigningCredential).where(SigningCredential.trading_account_id == account.id))).scalar_one_or_none()
        if old: await db.delete(old); await db.flush()
    blob = crypto.encrypt(body.agent_private_key, user_id=str(user.id), account_id=str(account.id))
    expires = datetime.fromtimestamp(verification.valid_until/1000, UTC) if verification.valid_until else None
    db.add(SigningCredential(trading_account_id=account.id, ciphertext_b64=blob.ciphertext_b64, nonce_b64=blob.nonce_b64, wrapped_dek_b64=blob.wrapped_dek_b64, wrap_nonce_b64=blob.wrap_nonce_b64, key_provider=blob.key_provider, key_reference=blob.key_reference, key_version=blob.key_version, agent_fingerprint=hashlib.sha256(verification.agent_address.encode()).hexdigest(), expires_at=expires, status=CredentialStatus.ACTIVE))
    await audit(db, action='TRADING_ACCOUNT_LINKED', actor_id=user.id, subject_id=user.id, ip_hash=hash_ip(request.client.host if request.client else None), after={'account': account_address[:8]+'…', 'agent': verification.agent_address[:8]+'…', 'expires_at': expires.isoformat() if expires else None})
    await db.commit(); return await _serialize_user(db, user)


@router.delete('/trading-account', dependencies=[Depends(require_csrf)], status_code=204)
async def unlink_trading_account(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    open_managed = (await db.execute(select(PositionLedger.id).where(
        PositionLedger.user_id == user.id, PositionLedger.managed.is_(True), PositionLedger.size != 0
    ).limit(1))).scalar_one_or_none()
    if open_managed:
        raise HTTPException(409, 'Close all HyperCopy-managed positions before removing the trading credential')
    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if account: await db.delete(account)
    user.copy_state = CopyState.PAUSED
    await audit(db, action='TRADING_ACCOUNT_UNLINKED', actor_id=user.id, subject_id=user.id)
    await db.commit()


@router.get('/risk-profile')
async def get_risk(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one(); return {c.name: getattr(row, c.name) for c in row.__table__.columns if c.name not in {'id','user_id','created_at','updated_at'}}


@router.put('/risk-profile', dependencies=[Depends(require_csrf)])
async def put_risk(body: RiskProfileIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one()
    values = body.model_dump()
    ent = await entitlement(db, user)
    limits = ent.get('limits') or {}
    if 'max_multiplier' in limits:
        values['multiplier'] = min(values['multiplier'], __import__('decimal').Decimal(str(limits['max_multiplier'])))
    if 'max_notional_per_trade' in limits:
        values['max_notional_per_trade'] = min(values['max_notional_per_trade'], __import__('decimal').Decimal(str(limits['max_notional_per_trade'])))
    if 'max_positions' in limits:
        values['max_positions'] = min(values['max_positions'], int(limits['max_positions']))
    for k, v in values.items(): setattr(row, k, v)
    await audit(db, action='RISK_PROFILE_UPDATED', actor_id=user.id, subject_id=user.id, after={k: str(v) if hasattr(v, 'as_tuple') else v for k, v in values.items()})
    await db.commit(); return await get_risk(user, db)


@router.post('/copy/pause', dependencies=[Depends(require_csrf)])
async def pause(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    user.copy_state = CopyState.PAUSED; await audit(db, action='COPY_PAUSED', actor_id=user.id, subject_id=user.id); await db.commit(); return await _serialize_user(db, user)


@router.post('/copy/resume', dependencies=[Depends(require_csrf)])
async def resume(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rs = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
    if rs and rs.state != RiskHalt.NORMAL: raise HTTPException(409, f'Cannot resume while {rs.state.value} is active')
    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if not account: raise HTTPException(409, 'Connect a Hyperliquid trading account first')
    # Reconcile against the exchange before enabling new exposure. While the
    # user is still PAUSED/SHADOW, any reduction jobs are safe and openings are
    # not allowed until this transaction flips the state to ACTIVE.
    try:
        hl = _hl()
        mp, meq, mids = await master_snapshot(hl)
        await reconcile_user(db, hl, user, master_positions=mp, master_equity=meq, mids=mids)
    except Exception as exc:
        raise HTTPException(503, 'Reconciliation must succeed before copytrading can resume') from exc
    user.copy_state = CopyState.ACTIVE
    await audit(db, action='COPY_RESUMED', actor_id=user.id, subject_id=user.id)
    await db.commit()
    return await _serialize_user(db, user)


@router.post('/copy/close-positions', dependencies=[Depends(require_csrf)])
async def close_positions(body: ClosePositionsIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(PositionLedger).where(PositionLedger.user_id == user.id, PositionLedger.managed.is_(True), PositionLedger.size != 0))).scalars().all()
    count = 0
    for row in rows:
        db.add(CopyJob(user_id=user.id, asset=row.asset, origin='CLOSE_ALL', state='QUEUED', correlation_id=__import__('uuid').uuid4().hex, context={'master_position': '0', 'master_equity': '1', 'mark_price': '0'})); count += 1
    await audit(db, action='CLOSE_POSITIONS_REQUESTED', actor_id=user.id, subject_id=user.id, reason=body.reason, after={'jobs': count}); await db.commit(); return {'queued': count}
