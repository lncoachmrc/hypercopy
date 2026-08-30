from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum

from app.engine.sizing import EXCHANGE_MIN_NOTIONAL, OrderIntent, SizingResult, round_size


class RiskAction(str, Enum):
    ALLOW = 'ALLOW'
    TRIM = 'TRIM'
    DENY = 'DENY'
    SKIP = 'SKIP'


@dataclass(frozen=True, slots=True)
class RiskContext:
    user_active: bool = True
    entitlement_active: bool = True
    credential_active: bool = True
    user_paused: bool = False
    global_pause: bool = False
    emergency_stop: bool = False
    close_only: bool = False
    asset_allowed: bool = True
    drawdown_halt: bool = False
    daily_loss_halt: bool = False
    near_liquidation: bool = False
    data_stale: bool = False
    current_total_exposure: Decimal = Decimal(0)
    current_asset_exposure: Decimal = Decimal(0)
    free_margin: Decimal = Decimal('999999999')
    account_equity: Decimal = Decimal('999999999')
    current_leverage: Decimal = Decimal(0)
    open_positions: int = 0
    is_new_market: bool = False
    max_notional_per_trade: Decimal = Decimal('1000')
    max_total_exposure: Decimal = Decimal('5000')
    max_asset_exposure: Decimal = Decimal('2500')
    max_leverage: Decimal = Decimal('3')
    max_positions: int = 5


@dataclass(frozen=True, slots=True)
class RiskDecision:
    action: RiskAction
    plan: SizingResult
    reason: str | None = None


def evaluate(plan: SizingResult, ctx: RiskContext) -> RiskDecision:
    if not plan.actionable:
        return RiskDecision(RiskAction.SKIP, plan, plan.reason)
    reducing = plan.intent in (OrderIntent.REDUCE, OrderIntent.CLOSE) or plan.reduce_only

    # Exposure-reducing actions intentionally bypass business/risk halts.
    if not reducing:
        for blocked, reason in [
            (ctx.emergency_stop, 'Emergency stop is active'),
            (ctx.global_pause, 'Global pause is active'),
            (ctx.user_paused, 'User paused copytrading'),
            (not ctx.user_active, 'Account is suspended'),
            (not ctx.entitlement_active, 'Subscription is not entitled'),
            (ctx.close_only, 'Account is close-only'),
            (not ctx.asset_allowed, 'Asset is not permitted'),
            (ctx.drawdown_halt, 'Drawdown halt is active'),
            (ctx.daily_loss_halt, 'Daily loss halt is active'),
            (ctx.near_liquidation, 'Liquidation distance is below safety floor'),
        ]:
            if blocked:
                return RiskDecision(RiskAction.DENY, plan, reason)

    if not ctx.credential_active:
        return RiskDecision(RiskAction.DENY, plan, 'Trading credential is unavailable')
    if ctx.data_stale:
        return RiskDecision(RiskAction.DENY, plan, 'Account data is stale; refresh required')

    # A reversal is deliberately two-phase: flattening the current side is a
    # reduction and stays allowed, but the secondary opening leg must pass the
    # complete runtime Risk Engine again using the post-close book. This prevents
    # plan/entitlement downgrades, stale queued context, or per-trade/exposure
    # caps from being bypassed by a close-then-open sequence.
    if plan.intent is OrderIntent.REVERSE and plan.secondary is not None:
        total_after_close = max(
            ctx.current_total_exposure - ctx.current_asset_exposure,
            Decimal(0),
        )
        open_after_close = max(ctx.open_positions - 1, 0)
        leverage_after_close = (
            total_after_close / ctx.account_equity
            if ctx.account_equity > 0
            else Decimal(999)
        )
        secondary_ctx = replace(
            ctx,
            current_total_exposure=total_after_close,
            current_asset_exposure=Decimal(0),
            current_leverage=leverage_after_close,
            open_positions=open_after_close,
            is_new_market=True,
        )
        secondary_decision = evaluate(plan.secondary, secondary_ctx)
        if secondary_decision.action in {RiskAction.ALLOW, RiskAction.TRIM}:
            return RiskDecision(
                RiskAction.ALLOW,
                replace(plan, secondary=secondary_decision.plan),
                secondary_decision.reason,
            )
        return RiskDecision(
            RiskAction.ALLOW,
            replace(plan, secondary=None),
            f'Reversal open leg suppressed: {secondary_decision.reason or secondary_decision.action.value}',
        )

    if reducing:
        return RiskDecision(RiskAction.ALLOW, plan)

    # A plan/profile downgrade can leave more positions open than the new cap.
    # In that state, permit only exposure reductions until the book is compliant;
    # otherwise existing over-cap markets could keep increasing indefinitely.
    if ctx.open_positions > ctx.max_positions:
        return RiskDecision(
            RiskAction.DENY,
            plan,
            'Open positions exceed maximum; reductions only until compliant',
        )
    if ctx.is_new_market and ctx.open_positions >= ctx.max_positions:
        return RiskDecision(RiskAction.DENY, plan, 'Maximum open positions reached')

    caps = [ctx.max_notional_per_trade]
    caps.append(max(ctx.max_total_exposure - ctx.current_total_exposure, Decimal(0)))
    caps.append(max(ctx.max_asset_exposure - ctx.current_asset_exposure, Decimal(0)))
    # Margin and leverage are separate constraints. `free_margin` is collateral;
    # approximate supported notional with the configured/asset leverage, while
    # the leverage headroom guarantees the resulting total book does not exceed
    # max_leverage even when current leverage is still below the ceiling.
    caps.append(max(ctx.free_margin, Decimal(0)) * max(ctx.max_leverage, Decimal(1)))
    leverage_headroom = max(ctx.max_leverage * max(ctx.account_equity, Decimal(0)) - ctx.current_total_exposure, Decimal(0))
    caps.append(leverage_headroom)
    allowed_notional = min(caps)
    if allowed_notional <= 0:
        return RiskDecision(RiskAction.DENY, plan, 'Exposure or margin limit reached')
    if allowed_notional < EXCHANGE_MIN_NOTIONAL:
        return RiskDecision(
            RiskAction.DENY,
            plan,
            f'Risk headroom ${allowed_notional:.2f} is below exchange minimum ${EXCHANGE_MIN_NOTIONAL:.0f}',
        )
    if plan.notional <= allowed_notional:
        return RiskDecision(RiskAction.ALLOW, plan)
    if plan.notional <= 0:
        return RiskDecision(RiskAction.SKIP, plan, 'No notional to execute')

    ratio = allowed_notional / plan.notional
    raw_trimmed_size = plan.order_size * ratio
    exponent = plan.order_size.as_tuple().exponent
    sz_decimals = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    trimmed_size = round_size(raw_trimmed_size, sz_decimals)
    if trimmed_size <= 0:
        return RiskDecision(RiskAction.DENY, plan, 'Risk cap rounds below the minimum executable lot')

    unit_price = plan.notional / plan.order_size
    trimmed_notional = trimmed_size * unit_price
    if trimmed_notional < EXCHANGE_MIN_NOTIONAL:
        return RiskDecision(
            RiskAction.DENY,
            plan,
            f'Rounded risk-limited order ${trimmed_notional:.2f} is below exchange minimum ${EXCHANGE_MIN_NOTIONAL:.0f}',
        )

    trimmed = replace(plan, order_size=trimmed_size, notional=trimmed_notional)
    return RiskDecision(RiskAction.TRIM, trimmed, f'Trimmed to risk cap ${allowed_notional:.2f}')
