from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Budget, Priority, WeightedRateLimiter
from app.api.deps import require_role
from app.core.config import settings
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import Role, TradingAccount, User
from app.services.networking import user_network_state

router = APIRouter(prefix="/admin", tags=["admin"])
admin = require_role(Role.ADMIN, Role.SUPERADMIN)


def _follower_adapter(network: str) -> HyperliquidAdapter:
    limiter = WeightedRateLimiter(
        redis_client(),
        Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN),
    )
    return HyperliquidAdapter(limiter, network=network)


@router.get("/users/{user_id}/hyperliquid-rate-limit")
async def hyperliquid_rate_limit_diagnostic(
    user_id: uuid.UUID,
    actor: User = Depends(admin),
    db: AsyncSession = Depends(get_db),
):
    """Read official and local per-address action-quota evidence for an admin."""

    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    account = (
        await db.execute(
            select(TradingAccount).where(TradingAccount.user_id == target.id)
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(409, "Follower has no Hyperliquid trading account")

    network = (await user_network_state(db, target.id)).network
    hl = _follower_adapter(network)
    try:
        official = await hl.user_rate_limit(
            account.account_address,
            priority=Priority.DIAGNOSTIC,
        )
        if hl.address_limits is not None:
            await hl.address_limits.record_exchange_snapshot(
                account.account_address,
                official,
            )
        local = await hl.address_rate_limit_snapshot(account.account_address)
    except Exception as exc:
        raise HTTPException(
            502,
            f"Hyperliquid rate-limit diagnostic failed: {type(exc).__name__}: {exc}",
        ) from exc

    return {
        "user_id": str(target.id),
        "network": network,
        "address": account.account_address.lower(),
        "official": {
            "cumVlm": official.get("cumVlm"),
            "nRequestsUsed": official.get("nRequestsUsed"),
            "nRequestsCap": official.get("nRequestsCap"),
            "nRequestsSurplus": official.get("nRequestsSurplus"),
        },
        "local": local,
    }
