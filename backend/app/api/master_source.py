from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Budget, Priority, WeightedRateLimiter
from app.api.deps import current_user
from app.core.config import settings
from app.core.security import normalize_address
from app.db.redis import redis_client
from app.models.entities import User


router = APIRouter(tags=['master-source'])


def _is_master_source(user: User) -> bool:
    if not settings.HYPERLIQUID_MASTER_ADDRESS:
        return False
    return normalize_address(user.auth_wallet) == normalize_address(settings.HYPERLIQUID_MASTER_ADDRESS)


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or '0'))
    except Exception:
        return Decimal(0)


@router.get('/master-source/status')
async def master_source_status(user: User = Depends(current_user)):
    if not _is_master_source(user):
        raise HTTPException(403, 'This account is not the configured master source')

    limiter = WeightedRateLimiter(
        redis_client(),
        Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN),
    )
    # The canonical source is MAINNET regardless of any follower network chosen
    # in the UI. This endpoint is strictly read-only and never signs an action.
    hl = HyperliquidAdapter(limiter, network='mainnet')
    try:
        snapshot = await hl.account_snapshot(
            settings.HYPERLIQUID_MASTER_ADDRESS,
            priority=Priority.RECONCILE,
        )
    except Exception as exc:
        raise HTTPException(503, f'Master MAINNET snapshot unavailable: {type(exc).__name__}: {exc}') from exc

    positions = []
    for row in snapshot.perp_state.get('assetPositions', []):
        position = row.get('position', row)
        size = _decimal(position.get('szi'))
        if size == 0:
            continue
        leverage = position.get('leverage') or {}
        positions.append({
            'asset': str(position.get('coin') or ''),
            'size': str(size),
            'entry_price': str(position.get('entryPx') or ''),
            'position_value': str(position.get('positionValue') or '0'),
            'unrealized_pnl': str(position.get('unrealizedPnl') or '0'),
            'leverage': int(_decimal(leverage.get('value') or 1)),
            'margin_mode': 'isolated' if str(leverage.get('type') or '').lower() == 'isolated' else 'cross',
        })

    return {
        'mode': 'MASTER_SOURCE_READ_ONLY',
        'network': 'mainnet',
        'address': settings.HYPERLIQUID_MASTER_ADDRESS,
        'account_value': float(snapshot.account_value),
        'collateral_balance': float(snapshot.collateral_balance),
        'unrealized_pnl': float(snapshot.unrealized_pnl),
        'free_margin': float(snapshot.free_margin),
        'positions': positions,
        'follower_controls_enabled': False,
    }
