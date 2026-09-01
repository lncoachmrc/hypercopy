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
from app.services.entitlement import entitlement
from app.services.execution import live_trading_allowed
from app.services.master_source_identity import MASTER_SOURCE_FOLLOWER_BLOCK_REASON, is_master_source_user
from app.services.networking import user_network_state
from app.services.queue import repair_stream
from app.services.reconcile import (
    master_snapshot_started_order,
    observed_master_mids,
    reconcile_user,
)

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


def _activation_entitlement_error(ent: dict) -> str | None:
    if ent.get('entitled'):
        return None

    if ent.get('portfolio_limit_exceeded'):
        equity = ent.get('portfolio_equity')
        limit = ent.get('portfolio_limit_usd')
        if equity is not None and limit is not None:
            return (
                f'Current plan does not cover this portfolio size '
                f'(equity ${float(equity):.2f} > plan limit ${float(limit):.2f}). '
                'Choose a plan that covers the account before activating the strategy.'
            )
        return 'Current plan does not cover this portfolio size. Choose a higher plan before activating the strategy.'

    status = str(ent.get('status') or 'none').lower()
    if status == 'none':
        return 'Activate a plan before activating the strategy.'
    if status == 'complimentary':
        return 'The complimentary plan is no longer entitled. Restore its 100% personal discount or activate another plan.'
    return f'Subscription is not entitled ({status}). Activate or renew a plan before activating the strategy.'


@router.post('/copy/resume', dependencies=[Depends(require_csrf)])
async def resume_copy_immediate(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Activate strategy execution on the user's selected Hyperliquid network.

    Activation preflights fresh follower equity, commercial entitlement, source
    and selected follower network, refuses to start with old pending jobs,
    preserves the independent mainnet live-trading gates, and rolls back to
    PAUSED if the initial ACTIVE reconciliation fails.
    """
    if is_master_source_user(user):
        raise HTTPException(409, MASTER_SOURCE_FOLLOWER_BLOCK_REASON)

    network = (await user_network_state(db, user.id)).network
    if not await live_trading_allowed(db, network):
        raise HTTPException(409, 'Mainnet live-trading gate is closed')

    rs = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
    if rs and rs.state != RiskHalt.NORMAL:
        raise HTTPException(409, f'Cannot resume while {rs.state.value} is active')

    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if not account:
        raise HTTPException(409, 'Connect a Hyperliquid trading account first')
    if not settings.HYPERLIQUID_MASTER_ADDRESS:
        raise HTTPException(409, 'Strategy source account is not configured')

    pending = (await db.execute(select(CopyJob.id).where(
        CopyJob.user_id == user.id,
        CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]),
    ).limit(1))).scalar_one_or_none()
    if pending:
        raise HTTPException(409, 'Pending strategy jobs exist; wait for Queue to return to 0 before activation')

    limiter = _limiter()
    master_hl = HyperliquidAdapter(limiter, network=settings.master_network)
    follower_hl = HyperliquidAdapter(limiter, network=network)

    try:
        # Allocate the causal boundary before reading master state. Activation
        # must never report success with unversioned jobs that the latest-intent
        # fence will immediately quarantine.
        snapshot_started_order = await master_snapshot_started_order(required=True)
        source_snapshot = await master_hl.account_snapshot(
            settings.HYPERLIQUID_MASTER_ADDRESS,
            priority=Priority.RECONCILE,
        )
        follower_snapshot = await follower_hl.account_snapshot(
            account.account_address,
            priority=Priority.RECONCILE,
        )
        master_positions = _positions(source_snapshot.perp_state)
        master_configs = position_configs(source_snapshot.perp_state)
        master_mids = observed_master_mids(await master_hl.mids(), snapshot_started_order)
        follower_mids = master_mids if settings.master_network == network else await follower_hl.mids()
    except Exception as exc:
        raise HTTPException(503, f'Strategy activation preflight failed: {type(exc).__name__}: {exc}') from exc

    ent = await entitlement(
        db,
        user,
        portfolio_equity_override=follower_snapshot.account_value,
    )
    entitlement_error = _activation_entitlement_error(ent)
    if entitlement_error:
        raise HTTPException(409, entitlement_error)

    activation_started = datetime.now(UTC)
    user.copy_state = CopyState.ACTIVE
    await audit(
        db,
        action='COPY_ACTIVATION_STARTED',
        actor_id=user.id,
        subject_id=user.id,
        after={
            'master_network': settings.master_network,
            'follower_network': network,
            'master_positions': len([x for x in master_positions.values() if x != 0]),
            'follower_equity': str(follower_snapshot.account_value),
            'entitlement_plan': ent.get('commercial_plan') or ent.get('plan'),
            'entitlement_status': ent.get('status'),
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
        published = await repair_stream(redis_client(), db)
        await audit(
            db,
            action='COPY_RESUMED',
            actor_id=user.id,
            subject_id=user.id,
            after={
                'master_network': settings.master_network,
                'follower_network': network,
                'reconciliation': result,
                'stream_published': published,
            },
        )
        await db.commit()
        return {
            'ok': True,
            'copy_state': user.copy_state.value,
            'network': network,
            'master_positions': len([x for x in master_positions.values() if x != 0]),
            'stream_published': published,
            'reconciliation': result,
        }
    except Exception as exc:
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
            after={'follower_network': network},
        )
        await db.commit()
        raise HTTPException(503, f'Strategy activation failed and the account was paused: {type(exc).__name__}: {exc}') from exc
