from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Budget, Priority, WeightedRateLimiter
from app.api.deps import require_csrf, require_role
from app.core.config import settings
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import AuditLog, CopyJob, CopyState, RiskHalt, RiskState, Role, SystemFlag, TradingAccount, User
from app.schemas.admin import AdminAction, AdminReconcile
from app.services.audit import audit
from app.services.metrics import system_snapshot
from app.services.reconcile import master_snapshot, reconcile_user

router = APIRouter(prefix='/admin', tags=['admin'])
admin = require_role(Role.ADMIN, Role.SUPERADMIN)
superadmin = require_role(Role.SUPERADMIN)


def _master_adapter() -> HyperliquidAdapter:
    limiter = WeightedRateLimiter(redis_client(), Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN))
    return HyperliquidAdapter(limiter, network=settings.master_network)


@router.get('/system')
async def system(user: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    limiter = WeightedRateLimiter(redis_client(), Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN))
    try: rate = await limiter.snapshot()
    except Exception: rate = {'status':'redis_unavailable'}
    flags = (await db.execute(select(SystemFlag))).scalars().all()
    data = await system_snapshot(db, rate)
    data['flags'] = {f.slug: f.enabled for f in flags}
    data['master_network'] = settings.master_network
    data['follower_network'] = settings.follower_network
    return data


@router.get('/master-state')
async def master_state(user: User = Depends(admin)):
    """Read the configured master directly from Hyperliquid on demand.

    This endpoint is intentionally not folded into /admin/system because the
    Control Room polls /system every 10 seconds. Exchange state should be read
    only when an operator explicitly asks for it, preserving the shared API
    rate budget.
    """
    if not settings.HYPERLIQUID_MASTER_ADDRESS:
        raise HTTPException(409, 'HYPERLIQUID_MASTER_ADDRESS is not configured')
    hl = _master_adapter()
    try:
        snapshot = await hl.account_snapshot(settings.HYPERLIQUID_MASTER_ADDRESS, priority=Priority.MASTER_STATE)
        positions = []
        for row in snapshot.perp_state.get('assetPositions', []):
            position = row.get('position', row)
            size = Decimal(str(position.get('szi', '0') or '0'))
            if size == 0:
                continue
            positions.append({
                'asset': str(position.get('coin') or ''),
                'size': str(size),
                'unrealized_pnl': str(position.get('unrealizedPnl') or '0'),
            })
        positions.sort(key=lambda x: x['asset'])
        return {
            'network': settings.master_network,
            'address': settings.HYPERLIQUID_MASTER_ADDRESS,
            'account_mode': snapshot.abstraction,
            'equity': str(snapshot.account_value),
            'free_margin': str(snapshot.free_margin),
            'open_positions': len(positions),
            'positions': positions,
        }
    except Exception as exc:
        raise HTTPException(502, f'Master state read failed: {type(exc).__name__}: {exc}') from exc


@router.get('/users')
async def users(user: User = Depends(admin), db: AsyncSession = Depends(get_db), limit: int = 100, offset: int = 0, q: str | None = None):
    query = select(User)
    if q: query = query.where(User.auth_wallet.ilike(f'%{q}%'))
    rows = (await db.execute(query.order_by(User.created_at.desc()).offset(offset).limit(min(limit,200)))).scalars().all()
    return [{'id':str(x.id),'auth_wallet':x.auth_wallet,'role':x.role.value,'state':x.state.value,'copy_state':x.copy_state.value,'created_at':x.created_at} for x in rows]


@router.get('/users/{user_id}')
async def user_detail(user_id: uuid.UUID, actor: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    target = await db.get(User, user_id)
    if not target: raise HTTPException(404, 'User not found')
    return {'id':str(target.id),'auth_wallet':target.auth_wallet,'role':target.role.value,'state':target.state.value,'copy_state':target.copy_state.value,'manual_trade_policy':target.manual_trade_policy.value}


@router.post('/users/{user_id}/pause', dependencies=[Depends(require_csrf)])
async def pause_user(user_id: uuid.UUID, body: AdminAction, actor: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    target=await db.get(User,user_id)
    if not target: raise HTTPException(404,'User not found')
    target.copy_state=CopyState.PAUSED; await audit(db,action='ADMIN_USER_PAUSE',actor_id=actor.id,subject_id=target.id,reason=body.reason); await db.commit(); return {'ok':True}


@router.post('/users/{user_id}/resume', dependencies=[Depends(require_csrf)])
async def resume_user(user_id: uuid.UUID, body: AdminAction, actor: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    target=await db.get(User,user_id)
    if not target: raise HTTPException(404,'User not found')
    rs=(await db.execute(select(RiskState).where(RiskState.user_id==target.id))).scalar_one_or_none()
    if rs and rs.state != RiskHalt.NORMAL:
        raise HTTPException(409,f'Cannot resume while {rs.state.value} is active')
    account=(await db.execute(select(TradingAccount).where(TradingAccount.user_id==target.id))).scalar_one_or_none()
    if not account: raise HTTPException(409,'Follower has no Hyperliquid trading account')
    limiter=WeightedRateLimiter(redis_client(),Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN))
    master_hl=HyperliquidAdapter(limiter,network=settings.master_network)
    follower_hl=HyperliquidAdapter(limiter,network=settings.follower_network)
    try:
        mp,meq,master_mids=await master_snapshot(master_hl)
        follower_mids=master_mids if settings.master_network == settings.follower_network else await follower_hl.mids()
        await reconcile_user(
            db,follower_hl,target,
            master_positions=mp,master_equity=meq,mids=follower_mids,master_mids=master_mids,
        )
    except Exception as exc:
        raise HTTPException(503,'Reconciliation must succeed before admin resume') from exc
    target.copy_state=CopyState.ACTIVE
    await audit(db,action='ADMIN_USER_RESUME',actor_id=actor.id,subject_id=target.id,reason=body.reason,after={'master_network':settings.master_network,'follower_network':settings.follower_network})
    await db.commit(); return {'ok':True}


@router.post('/users/{user_id}/reconcile', dependencies=[Depends(require_csrf)])
async def queue_reconcile(user_id: uuid.UUID, body: AdminReconcile, actor: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    target=await db.get(User,user_id)
    if not target: raise HTTPException(404,'User not found')
    db.add(CopyJob(user_id=target.id,asset='__RECONCILE__',origin='ADMIN_RECONCILE',state='QUEUED',correlation_id=uuid.uuid4().hex,context={'reason':body.reason}))
    await audit(db,action='ADMIN_RECONCILE_REQUESTED',actor_id=actor.id,subject_id=target.id,reason=body.reason); await db.commit(); return {'queued':True}


async def _flag(db: AsyncSession, slug: str, enabled: bool, actor: User, reason: str):
    flag=await db.get(SystemFlag,slug)
    if not flag: flag=SystemFlag(slug=slug,enabled=enabled); db.add(flag)
    flag.enabled=enabled; flag.reason=reason; flag.updated_by=actor.id
    await audit(db,action=f'SYSTEM_FLAG_{slug.upper()}',actor_id=actor.id,reason=reason,after={'enabled':enabled}); await db.commit()


@router.post('/system/pause', dependencies=[Depends(require_csrf)])
async def global_pause(body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    await _flag(db,'global_pause',True,actor,body.reason); return {'ok':True}


@router.post('/system/emergency-stop', dependencies=[Depends(require_csrf)])
async def emergency(body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    if body.confirmation != 'EMERGENCY STOP': raise HTTPException(422,'Confirmation must be EMERGENCY STOP')
    await _flag(db,'emergency_stop',True,actor,body.reason); return {'ok':True,'note':'Open/increase is blocked; exits remain permitted'}


@router.post('/system/live-trading', dependencies=[Depends(require_csrf)])
async def live_trading(body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    if body.confirmation != 'ENABLE MAINNET': raise HTTPException(422,'Confirmation must be ENABLE MAINNET')
    if settings.follower_network != 'mainnet' or not settings.ENABLE_LIVE_TRADING: raise HTTPException(409,'Environment gates 1/2 are not enabled')
    await _flag(db,'live_trading',True,actor,body.reason); return {'ok':True}


@router.post('/system/live-trading/disable', dependencies=[Depends(require_csrf)])
async def disable_live_trading(body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    await _flag(db,'live_trading',False,actor,body.reason)
    return {'ok':True}


@router.post('/system/resume', dependencies=[Depends(require_csrf)])
async def resume_system(body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    await _flag(db,'global_pause',False,actor,body.reason); await _flag(db,'emergency_stop',False,actor,body.reason); return {'ok':True}


@router.get('/audit')
async def audit_log(actor: User = Depends(admin), db: AsyncSession = Depends(get_db), limit: int = 100, offset: int = 0):
    rows=(await db.execute(select(AuditLog).order_by(AuditLog.ts.desc()).offset(offset).limit(min(limit,200)))).scalars().all()
    return [{'id':str(x.id),'action':x.action,'actor_id':str(x.actor_id) if x.actor_id else None,'subject_id':str(x.subject_id) if x.subject_id else None,'reason':x.reason,'ts':x.ts,'before':x.before,'after':x.after} for x in rows]
