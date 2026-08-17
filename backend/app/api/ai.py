from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import PositionLedger, User
from app.services.ai_intelligence import provider_chain, read_ai_intelligence

router = APIRouter(prefix='/ai', tags=['ai'])


@router.get('/intelligence')
async def intelligence(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    state = await read_ai_intelligence(db)
    ledgers = (await db.execute(
        select(PositionLedger).where(PositionLedger.user_id == user.id, PositionLedger.managed.is_(True))
    )).scalars().all()

    total_target = Decimal(0)
    executable_target = Decimal(0)
    executable_positions = 0
    below_min_positions = 0
    for row in ledgers:
        notional = abs((row.target_size or Decimal(0)) * (row.mark_price or Decimal(0)))
        total_target += notional
        if notional >= Decimal('10'):
            executable_target += notional
            executable_positions += 1
        elif notional > 0:
            below_min_positions += 1

    coverage = float(executable_target / total_target) if total_target > 0 else None
    safe_chain = [{'provider': provider, 'model': model} for provider, model in provider_chain()]
    return {
        **state,
        'configured_chain': safe_chain,
        'capital_efficiency': {
            'managed_positions': len(ledgers),
            'executable_positions': executable_positions,
            'below_min_positions': below_min_positions,
            'target_notional': str(total_target),
            'executable_target_notional': str(executable_target),
            'coverage_pct': round(coverage * 100, 2) if coverage is not None else None,
        },
        'execution_influence': False,
        'safety': 'LLM is advisory/shadow. Deterministic sizing and Risk Engine remain authoritative.',
    }
