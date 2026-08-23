from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import SystemFlag
from app.services.ai_intelligence import AI_FLAG_SLUG

AI_EXECUTION_FLAG_SLUG = 'ai:execution_influence'
MAX_AI_BUFFER_PCT = Decimal('0.30')
MIN_AI_EXECUTION_FACTOR = Decimal('0.70')


@dataclass(frozen=True, slots=True)
class AiExecutionPolicy:
    requested_mode: str
    effective_mode: str
    requested: bool
    effective: bool
    factor: Decimal
    buffer_pct: Decimal
    status: str
    fallback_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            'requested_mode': self.requested_mode,
            'effective_mode': self.effective_mode,
            'execution_influence_requested': self.requested,
            'execution_influence': self.effective,
            'execution_factor': str(self.factor),
            'execution_buffer_pct': str(self.buffer_pct),
            'fallback_reason': self.fallback_reason,
        }


def _bounded_decimal(value, *, default: Decimal = Decimal(0)) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = default
    return max(Decimal(0), min(parsed, MAX_AI_BUFFER_PCT))


def derive_ai_execution_policy(state: dict | None, *, requested: bool) -> AiExecutionPolicy:
    state = state if isinstance(state, dict) else {}
    status = str(state.get('status') or 'pending').lower()
    analysis = state.get('analysis') if isinstance(state.get('analysis'), dict) else {}
    capital_policy = analysis.get('capital_policy') if isinstance(analysis.get('capital_policy'), dict) else {}
    buffer_pct = _bounded_decimal(capital_policy.get('buffer_pct', 0))

    fallback_reason = None
    effective = requested
    if requested and status != 'ok':
        effective = False
        fallback_reason = f'AI intelligence status is {status}; execution influence falls back to SHADOW.'
    elif requested and not capital_policy:
        effective = False
        fallback_reason = 'AI capital policy is unavailable; execution influence falls back to SHADOW.'

    factor = max(MIN_AI_EXECUTION_FACTOR, Decimal(1) - buffer_pct) if effective else Decimal(1)
    return AiExecutionPolicy(
        requested_mode='on' if requested else 'shadow',
        effective_mode='on' if effective else 'shadow',
        requested=requested,
        effective=effective,
        factor=factor,
        buffer_pct=buffer_pct if effective else Decimal(0),
        status=status,
        fallback_reason=fallback_reason,
    )


async def read_ai_execution_policy(db: AsyncSession) -> AiExecutionPolicy:
    mode_row = await db.get(SystemFlag, AI_EXECUTION_FLAG_SLUG)
    intelligence_row = await db.get(SystemFlag, AI_FLAG_SLUG)
    state = (intelligence_row.value or {}) if intelligence_row else {}
    return derive_ai_execution_policy(state, requested=bool(mode_row and mode_row.enabled))


async def set_ai_execution_mode(db: AsyncSession, *, enabled: bool, reason: str) -> AiExecutionPolicy:
    intelligence_row = await db.get(SystemFlag, AI_FLAG_SLUG)
    state = (intelligence_row.value or {}) if intelligence_row else {}
    candidate = derive_ai_execution_policy(state, requested=enabled)
    if enabled and not candidate.effective:
        raise ValueError(candidate.fallback_reason or 'AI intelligence is not ready for ON mode')

    now = datetime.now(UTC)
    row = await db.get(SystemFlag, AI_EXECUTION_FLAG_SLUG)
    value = {
        'requested_mode': 'on' if enabled else 'shadow',
        'updated_at': now.isoformat(),
    }
    if row is None:
        row = SystemFlag(
            slug=AI_EXECUTION_FLAG_SLUG,
            enabled=enabled,
            value=value,
            reason=reason,
        )
        db.add(row)
    else:
        row.enabled = enabled
        row.value = value
        row.reason = reason
    await db.flush()
    return await read_ai_execution_policy(db)
