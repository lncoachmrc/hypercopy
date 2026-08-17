from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter, position_configs
from app.adapters.ratelimit import Budget, Priority, WeightedRateLimiter
from app.api.deps import current_user
from app.core.config import settings
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import TradingAccount, User

router = APIRouter(tags=['user'])


def _limiter() -> WeightedRateLimiter:
    return WeightedRateLimiter(redis_client(), Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN))


@router.get('/position-leverage')
async def position_leverage(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return current per-asset leverage/margin mode directly from Hyperliquid.

    This endpoint is intentionally lightweight: it reads only clearinghouseState
    for the configured Master and the authenticated follower. It does not place
    orders or mutate risk/copy state. The Dashboard uses it at a low cadence so
    leverage visibility is not coupled to reconciliation/ledger timing.
    """
    if not settings.HYPERLIQUID_MASTER_ADDRESS:
        raise HTTPException(409, 'HYPERLIQUID_MASTER_ADDRESS is not configured')

    account = (await db.execute(
        select(TradingAccount).where(TradingAccount.user_id == user.id)
    )).scalar_one_or_none()
    if not account:
        return []

    limiter = _limiter()
    master_hl = HyperliquidAdapter(limiter, network=settings.master_network)
    follower_hl = HyperliquidAdapter(limiter, network=settings.follower_network)

    try:
        master_state, follower_state = await asyncio.gather(
            master_hl.user_state(settings.HYPERLIQUID_MASTER_ADDRESS, priority=Priority.DIAGNOSTIC),
            follower_hl.user_state(account.account_address, priority=Priority.DIAGNOSTIC),
        )
    except Exception as exc:
        raise HTTPException(502, f'Leverage state read failed: {type(exc).__name__}: {exc}') from exc

    master_configs = position_configs(master_state)
    follower_configs = position_configs(follower_state)
    assets = sorted(set(master_configs) | set(follower_configs))

    return [
        {
            'asset': asset,
            'master_leverage': master_configs[asset].leverage if asset in master_configs else None,
            'master_is_cross': master_configs[asset].is_cross if asset in master_configs else None,
            'follower_leverage': follower_configs[asset].leverage if asset in follower_configs else None,
            'follower_is_cross': follower_configs[asset].is_cross if asset in follower_configs else None,
        }
        for asset in assets
    ]
