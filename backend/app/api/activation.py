from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter, position_configs
from app.adapters.ratelimit import Budget, Priority, WeightedRateLimiter
from app.api.deps import current_user, require_csrf
from app.core.config import settings
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import CopyJob, CopyState, JobState, RiskHalt, RiskState, TradingAccount, User
from app.services.audit import audit
from app.services.queue import repair_stream
from app.services.reconcile import reconcile_user

router = APIRouter(tags=['activation'])


def _limiter() -> WeightedRateLimiter:
    return WeightedRateLimiter(redis_client(), Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN))


def _positions(state: dict) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in state.get('assetPositions', []):
        p = row.get('position', row)
        asset = str(p.get('coin') or '')
        if not asset:
            continue
        out[asset] = Decimal(str(p.get('szi', '0') or '0'))
    return out


@router.post('/copy/resume', dependencies=[Depends(require_csrf)])
async def resume_copy_immediate(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Activate TESTNET copy and create current multi-asset jobs immediately.

    This route intentionally precedes the legacy /copy/resume route.  It is
    restricted to TESTNET while the cross-network MAINNET->TESTNET pipeline is
    being validated.  Activation preflights both exchanges, refuses to start
    with old pending jobs, and rolls back to PAUSED if the initial ACTIVE
    reconciliation cannot be completed.
    """
    if settings.follower_network != 'testnet':
        raise HTTPException(409, 'Immediate activation handler is currently restricted to TESTNET')

    rs = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
    if rs and rs.state != RiskHalt.NORMAL:
        raise HTTPException(409, f'Cannot resume while {rs.state.value} is active')

    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if not account:
        raise HTTPException(409, 'Connect a Hyperliquid trading account first')
    if not settings.HYPERLIQUID_MASTER_ADDRESS:
        raise HTTPException(409, 'HYPERLIQUID_MASTER_ADDRESS is not configured')

    # Do not mix a fresh activation with historical work.  This keeps the
    # transition deterministic and prevents stale SHADOW jobs from executing.
    pending = (await db.execute(select(CopyJob.id).where(
        CopyJob.user_id == user.id,
        CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]),
    ).limit(1))).scalar_one_or_none()
    if pending:
        raise HTTPException(409, 'Pending copy jobs exist; wait for Queue to return to 0 before activation')

    limiter = _limiter()
    master_hl = HyperliquidAdapter(limiter, network=settings.master_network)
    follower_hl = HyperliquidAdapter(limiter, network=settings.follower_network)

    # Full preflight while the follower is still not ACTIVE.
    try:
        source_snapshot = await master_hl.account_snapshot(
            settings.HYPERLIQUID_MASTER_ADDRESS,
            priority=Priority.RECONCILE,
        )
        master_positions = _positions(source_snapshot.perp_state)
        master_configs = position_configs(source_snapshot.perp_state)
        master_mids = await master_hl.mids()
        follower_mids = master_mids if settings.master_network == settings.follower_network else await follower_hl.mids()
    except Exception as exc:
        raise HTTPException(503, f'Activation preflight failed: {type(exc).__name__}: {exc}') from exc

    activation_started = datetime.now(UTC)
    user.copy_state = CopyState.ACTIVE
    await audit(
        db,
        action='COPY_ACTIVATION_STARTED',
        actor_id=user.id,
        subject_id=user.id,
        after={
            'master_network': settings.master_network,
            'follower_network': settings.follower_network,
            'master_positions': len([x for x in master_positions.values() if x != 0]),
        },
    )
    await db.commit()

    try:
        result = await reconcile_user(
            db,
            follower_hl,
            user,
            master_positions=master_positions,
            master_equity=source_snapshot.account_value,
            mids=follower_mids,
            master_mids=master_mids,
            master_configs=master_configs,
        )
        # reconcile_user commits the durable jobs; publish them immediately so
        # activation does not wait for the next maintenance cycle.
        async with db.begin():
            published = await repair_stream(redis_client(), db)
        await audit(
            db,
            action='COPY_RESUMED',
            actor_id=user.id,
            subject_id=user.id,
            after={
                'master_network': settings.master_network,
                'follower_network': settings.follower_network,
                'reconciliation': result,
                'stream_published': published,
            },
        )
        await db.commit()
        return {
            'ok': True,
            'copy_state': user.copy_state.value,
            'master_positions': len([x for x in master_positions.values() if x != 0]),
            'stream_published': published,
            'reconciliation': result,
        }
    except Exception as exc:
        # Any jobs created by this failed activation are rendered inert before
        # returning the follower to PAUSED.
        rows = (await db.execute(select(CopyJob).where(
            CopyJob.user_id == user.id,
            CopyJob.created_at >= activation_started,
            CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]),
        ))).scalars().all()
        for job in rows:
            job.state = JobState.SKIPPED
            job.last_error = 'Activation rolled back before execution'
            job.owner = None
            job.locked_until = None
        user.copy_state = CopyState.PAUSED
        await audit(
            db,
            action='COPY_ACTIVATION_ROLLED_BACK',
            actor_id=user.id,
            subject_id=user.id,
            reason=f'{type(exc).__name__}: {exc}',
        )
        await db.commit()
        raise HTTPException(503, f'Activation failed and follower was paused: {type(exc).__name__}: {exc}') from exc
