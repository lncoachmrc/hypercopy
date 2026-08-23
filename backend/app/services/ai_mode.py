from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import CopyJob, SystemFlag
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


def _bounded_buffer(value, *, default: Decimal = Decimal(0)) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = default
    return max(Decimal(0), min(parsed, MAX_AI_BUFFER_PCT))


def _bounded_factor(value, *, default: Decimal = Decimal(1)) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = default
    return max(MIN_AI_EXECUTION_FACTOR, min(parsed, Decimal(1)))


def _factor_from_capital_policy(capital_policy: dict) -> Decimal | None:
    if not capital_policy:
        return None
    buffer_pct = _bounded_buffer(capital_policy.get('buffer_pct', 0))
    return max(MIN_AI_EXECUTION_FACTOR, Decimal(1) - buffer_pct)


def derive_ai_execution_policy(
    state: dict | None,
    *,
    requested: bool,
    fallback_factor: Decimal = Decimal(1),
) -> AiExecutionPolicy:
    state = state if isinstance(state, dict) else {}
    status = str(state.get('status') or 'pending').lower()
    analysis = state.get('analysis') if isinstance(state.get('analysis'), dict) else {}
    capital_policy = analysis.get('capital_policy') if isinstance(analysis.get('capital_policy'), dict) else {}
    latest_valid_factor = _factor_from_capital_policy(capital_policy)

    fallback_reason = None
    effective = requested
    if requested and status != 'ok':
        effective = False
        fallback_reason = (
            f'AI intelligence status is {status}; new AI influence is disabled and the last safe '
            'capital-allocation factor remains frozen.'
        )
    elif requested and not capital_policy:
        effective = False
        fallback_reason = (
            'AI capital policy is unavailable; new AI influence is disabled and the last safe '
            'capital-allocation factor remains frozen.'
        )

    if not requested:
        factor = Decimal(1)
    elif effective:
        factor = latest_valid_factor or Decimal(1)
    else:
        factor = _bounded_factor(fallback_factor)
        # A degraded intelligence row preserves the last successful analysis.
        # Never let a stale persisted fallback increase exposure above that
        # latest validated conservative factor.
        if latest_valid_factor is not None:
            factor = min(factor, latest_valid_factor)
    applied_buffer = Decimal(1) - factor

    return AiExecutionPolicy(
        requested_mode='on' if requested else 'shadow',
        effective_mode='on' if effective else 'shadow',
        requested=requested,
        effective=effective,
        factor=factor,
        buffer_pct=applied_buffer,
        status=status,
        fallback_reason=fallback_reason,
    )


def apply_ai_factor_to_job_context(context: dict | None) -> dict:
    """Persist the conservative target into the execution inputs themselves.

    Execution workers rebuild sizing from ``master_position`` rather than from
    the reconciliation ledger target. Scaling that persisted source position
    keeps reconciliation and execution on the exact same target while retaining
    the original source position for auditability.
    """
    out = dict(context or {})
    factor = _bounded_factor(out.get('ai_execution_factor', 1))
    if factor >= Decimal(1):
        return out
    try:
        master_position = Decimal(str(out.get('master_position', '0')))
    except (InvalidOperation, TypeError, ValueError):
        return out
    if master_position == 0:
        return out
    out['source_master_position'] = str(master_position)
    out['master_position'] = str(master_position * factor)
    out['ai_execution_factor_applied'] = True
    return out


@event.listens_for(CopyJob, 'before_insert')
def _apply_ai_factor_before_job_insert(_mapper, _connection, target: CopyJob) -> None:
    if target.origin != 'RECONCILE':
        return
    target.context = apply_ai_factor_to_job_context(target.context)


async def read_ai_execution_policy(db: AsyncSession) -> AiExecutionPolicy:
    mode_row = await db.get(SystemFlag, AI_EXECUTION_FLAG_SLUG)
    intelligence_row = await db.get(SystemFlag, AI_FLAG_SLUG)
    state = (intelligence_row.value or {}) if intelligence_row else {}
    mode_value = (mode_row.value or {}) if mode_row else {}
    fallback_factor = _bounded_factor(mode_value.get('last_valid_factor', 1))
    policy = derive_ai_execution_policy(
        state,
        requested=bool(mode_row and mode_row.enabled),
        fallback_factor=fallback_factor,
    )

    # Keep the persisted fallback aligned with every newly accepted healthy
    # policy. Callers already own the surrounding transaction; flush only.
    if mode_row and mode_row.enabled and policy.effective:
        previous_factor = _bounded_factor(mode_value.get('last_valid_factor', 1))
        previous_buffer = _bounded_buffer(mode_value.get('last_valid_buffer_pct', 0))
        if previous_factor != policy.factor or previous_buffer != policy.buffer_pct:
            mode_row.value = {
                **mode_value,
                'last_valid_factor': str(policy.factor),
                'last_valid_buffer_pct': str(policy.buffer_pct),
                'last_valid_updated_at': datetime.now(UTC).isoformat(),
            }
            await db.flush()
    return policy


async def set_ai_execution_mode(db: AsyncSession, *, enabled: bool, reason: str) -> AiExecutionPolicy:
    intelligence_row = await db.get(SystemFlag, AI_FLAG_SLUG)
    state = (intelligence_row.value or {}) if intelligence_row else {}
    candidate = derive_ai_execution_policy(state, requested=enabled)
    if enabled and not candidate.effective:
        raise ValueError(candidate.fallback_reason or 'AI intelligence is not ready for ON mode')

    now = datetime.now(UTC)
    row = await db.get(SystemFlag, AI_EXECUTION_FLAG_SLUG)
    previous = (row.value or {}) if row else {}
    value = {
        'requested_mode': 'on' if enabled else 'shadow',
        'last_valid_factor': str(candidate.factor) if enabled else previous.get('last_valid_factor', '1'),
        'last_valid_buffer_pct': str(candidate.buffer_pct) if enabled else previous.get('last_valid_buffer_pct', '0'),
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
