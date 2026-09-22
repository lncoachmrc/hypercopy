from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, require_csrf, require_role
from app.db.session import get_db
from app.models.entities import PositionLedger, Role, User
from app.services.ai_intelligence import provider_chain, read_ai_intelligence
from app.services.ai_mode import read_ai_execution_policy, set_ai_execution_mode
from app.services.ai_profit_exit import ProfitExitFeatureMode
from app.services.ai_profit_exit_mode import read_profit_exit_mode_state, set_profit_exit_mode
from app.services.audit import audit

router = APIRouter(prefix='/ai', tags=['ai'])
superadmin = require_role(Role.SUPERADMIN)


class AiModeChange(BaseModel):
    mode: Literal['shadow', 'on']
    reason: str = Field(default='Dashboard AI mode toggle', min_length=3, max_length=300)


class AiProfitExitModeChange(BaseModel):
    mode: Literal['OFF', 'SHADOW', 'ON']
    reason: str = Field(default='Dashboard AI Profit Exit mode toggle', min_length=3, max_length=300)


@router.get('/intelligence')
async def intelligence(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    state = await read_ai_intelligence(db)
    execution_policy = await read_ai_execution_policy(db)
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
    safety = (
        'AI policy is ON only as a bounded, risk-reducing capital-allocation input. '
        'The LLM never creates orders; deterministic sizing and Risk Engine remain authoritative.'
        if execution_policy.effective
        else 'LLM is advisory/shadow. Deterministic sizing and Risk Engine remain authoritative.'
    )
    return {
        **state,
        'mode': execution_policy.effective_mode,
        **execution_policy.as_dict(),
        'configured_chain': safe_chain,
        'capital_efficiency': {
            'managed_positions': len(ledgers),
            'executable_positions': executable_positions,
            'below_min_positions': below_min_positions,
            'target_notional': str(total_target),
            'executable_target_notional': str(executable_target),
            'coverage_pct': round(coverage * 100, 2) if coverage is not None else None,
        },
        'safety': safety,
    }


@router.post('/mode', dependencies=[Depends(require_csrf)])
async def update_mode(
    body: AiModeChange,
    actor: User = Depends(superadmin),
    db: AsyncSession = Depends(get_db),
):
    enabled = body.mode == 'on'
    try:
        policy = await set_ai_execution_mode(db, enabled=enabled, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    await audit(
        db,
        action='AI_EXECUTION_MODE_CHANGED',
        actor_id=actor.id,
        reason=body.reason,
        after={
            'requested_mode': policy.requested_mode,
            'effective_mode': policy.effective_mode,
            'execution_influence': policy.effective,
            'execution_factor': str(policy.factor),
            'execution_buffer_pct': str(policy.buffer_pct),
        },
    )
    await db.commit()
    return {
        'ok': True,
        'mode': policy.effective_mode,
        **policy.as_dict(),
        'takes_effect': 'next_reconciliation',
    }


@router.get('/profit-exit-mode')
async def profit_exit_mode(
    _actor: User = Depends(superadmin),
    db: AsyncSession = Depends(get_db),
):
    return await read_profit_exit_mode_state(db)


@router.post('/profit-exit-mode', dependencies=[Depends(require_csrf)])
async def update_profit_exit_mode(
    body: AiProfitExitModeChange,
    actor: User = Depends(superadmin),
    db: AsyncSession = Depends(get_db),
):
    before = await read_profit_exit_mode_state(db)
    state = await set_profit_exit_mode(
        db,
        mode=ProfitExitFeatureMode(body.mode),
        reason=body.reason,
        actor_id=actor.id,
    )
    await audit(
        db,
        action='AI_PROFIT_EXIT_MODE_CHANGED',
        actor_id=actor.id,
        reason=body.reason,
        before=before,
        after=state,
    )
    await db.commit()
    return {'ok': True, **state, 'takes_effect': 'next_profit_exit_evaluation'}
