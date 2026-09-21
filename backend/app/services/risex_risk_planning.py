"""Pure RISEx risk planning from authoritative provider reads.

This module intentionally stops at an immutable RISExOrderIntent. It has no
execution side effects: provider reads are supplied as already-fetched payloads,
and every decision is deterministic from those payloads plus TRAXION risk state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Mapping

from app.engine.risk import RiskAction, RiskContext, evaluate
from app.engine.sizing import (
    AssetSpec,
    FollowerState,
    MasterExposure,
    OrderIntent,
    SizingResult,
    plan,
)
from app.services.effective_risk import EffectiveRisk, resolve_effective_risk
from app.services.risex_order_preparation import RISExMarketMetadata, RISExOrderIntent


class RISExRiskPlanningDenied(RuntimeError):
    """A RISEx intent was refused locally before any execution material exists."""


@dataclass(frozen=True, slots=True)
class RISExPortfolioPosition:
    market_id: int
    size: Decimal
    mark_price: Decimal
    liquidation_price: Decimal | None


@dataclass(frozen=True, slots=True)
class RISExPortfolioSnapshot:
    usdc_balance: Decimal
    total_account_value: Decimal
    total_notional: Decimal
    free_collateral: Decimal | None
    in_liquidation: bool
    risk_level: str
    positions: tuple[RISExPortfolioPosition, ...]


@dataclass(frozen=True, slots=True)
class RISExRuntimeRiskFlags:
    user_active: bool = True
    entitlement_active: bool = True
    credential_active: bool = True
    user_paused: bool = False
    global_pause: bool = False
    emergency_stop: bool = False
    drawdown_halt: bool = False
    daily_loss_halt: bool = False
    near_liquidation: bool = False
    asset_allowed: bool = True
    data_stale: bool = False


@dataclass(frozen=True, slots=True)
class RISExProviderPositionLimitAssessment:
    provider_limit_base_conservative: Decimal | None
    risk_max_position_base: Decimal
    non_binding: bool
    unit_semantics_verified: bool = False


def _unwrap_data(payload: Mapping[str, Any], *, field: str) -> Mapping[str, Any]:
    data = payload.get("data")
    if data is None:
        return payload
    if not isinstance(data, Mapping):
        raise RISExRiskPlanningDenied(f"RISEx {field} data envelope is malformed")
    return data


def _decimal(
    value: object,
    *,
    field: str,
    allow_zero: bool = False,
    allow_negative: bool = False,
) -> Decimal:
    if value is None or isinstance(value, bool):
        raise RISExRiskPlanningDenied(f"RISEx {field} is indeterminate")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RISExRiskPlanningDenied(f"RISEx {field} is indeterminate") from exc
    if not parsed.is_finite():
        raise RISExRiskPlanningDenied(f"RISEx {field} is indeterminate")
    if not allow_negative and parsed < 0:
        raise RISExRiskPlanningDenied(f"RISEx {field} must not be negative")
    if not allow_zero and parsed == 0:
        raise RISExRiskPlanningDenied(f"RISEx {field} must be positive")
    return parsed


def _optional_nonnegative_decimal(value: object, *, field: str) -> Decimal | None:
    try:
        return _decimal(value, field=field, allow_zero=True)
    except RISExRiskPlanningDenied:
        return None


def _positive_int(value: object, *, field: str) -> int:
    if value is None or isinstance(value, bool):
        raise RISExRiskPlanningDenied(f"RISEx {field} is indeterminate")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RISExRiskPlanningDenied(f"RISEx {field} is indeterminate") from exc
    if parsed <= 0:
        raise RISExRiskPlanningDenied(f"RISEx {field} must be positive")
    return parsed


def _symbol_matches(row: Mapping[str, Any], symbol: str) -> bool:
    target = symbol.strip().upper()
    config = row.get("config")
    config_name = config.get("name") if isinstance(config, Mapping) else None
    for raw in (
        row.get("base_asset_symbol"),
        row.get("display_base_asset_symbol"),
        row.get("display_name"),
        row.get("underlying"),
        config_name,
    ):
        if not isinstance(raw, str):
            continue
        normalized = raw.strip().upper()
        if normalized == target or normalized.split("/", 1)[0] == target:
            return True
    return False


def parse_risex_risk_market(
    payload: Mapping[str, Any],
    *,
    symbol: str,
) -> RISExMarketMetadata:
    root = _unwrap_data(payload, field="markets")
    markets = root.get("markets")
    if not isinstance(markets, list):
        raise RISExRiskPlanningDenied("RISEx market metadata is missing")

    selected: Mapping[str, Any] | None = None
    for candidate in markets:
        if isinstance(candidate, Mapping) and _symbol_matches(candidate, symbol):
            selected = candidate
            break
    if selected is None:
        raise RISExRiskPlanningDenied(f"RISEx market for {symbol!r} is unknown")

    config = selected.get("config")
    if not isinstance(config, Mapping):
        raise RISExRiskPlanningDenied("RISEx market config is missing")

    active = selected.get("active")
    reduce_only = selected.get("reduce_only")
    unlocked = config.get("unlocked")
    if not isinstance(active, bool):
        raise RISExRiskPlanningDenied("RISEx market active flag is indeterminate")
    if not isinstance(reduce_only, bool):
        raise RISExRiskPlanningDenied("RISEx market reduce_only flag is indeterminate")
    if not isinstance(unlocked, bool):
        raise RISExRiskPlanningDenied("RISEx market unlocked flag is indeterminate")

    market_id = _positive_int(selected.get("market_id"), field="market_id")
    if market_id >= (1 << 16):
        raise RISExRiskPlanningDenied("RISEx market_id exceeds uint16")

    maintenance_raw = config.get("maintenance_margin_factor")
    return RISExMarketMetadata(
        market_id=market_id,
        step_size=_decimal(config.get("step_size"), field="step_size"),
        step_price=_decimal(config.get("step_price"), field="step_price"),
        min_order_size=_decimal(config.get("min_order_size"), field="min_order_size"),
        max_leverage=_decimal(config.get("max_leverage"), field="max_leverage"),
        active=active,
        reduce_only=reduce_only,
        unlocked=unlocked,
        max_position_size_raw=(
            None
            if selected.get("max_position_size") is None
            else _decimal(
                selected.get("max_position_size"),
                field="max_position_size",
            )
        ),
        mark_price=_decimal(selected.get("mark_price"), field="mark_price"),
        maintenance_margin_factor_raw=(
            None if maintenance_raw is None else str(maintenance_raw)
        ),
    )


def parse_risex_portfolio_details(
    payload: Mapping[str, Any],
) -> RISExPortfolioSnapshot:
    root = _unwrap_data(payload, field="portfolio")
    summary = root.get("summary")
    if not isinstance(summary, Mapping):
        raise RISExRiskPlanningDenied("RISEx portfolio summary is missing")

    in_liquidation = summary.get("in_liquidation")
    if not isinstance(in_liquidation, bool):
        raise RISExRiskPlanningDenied("RISEx in_liquidation is indeterminate")
    risk_level = summary.get("risk_level")
    if not isinstance(risk_level, str) or not risk_level.strip():
        raise RISExRiskPlanningDenied("RISEx risk_level is indeterminate")

    raw_positions = root.get("positions")
    if not isinstance(raw_positions, list):
        raise RISExRiskPlanningDenied("RISEx portfolio positions are missing")

    positions: list[RISExPortfolioPosition] = []
    for raw in raw_positions:
        if not isinstance(raw, Mapping):
            raise RISExRiskPlanningDenied("RISEx portfolio position is malformed")
        liquidation = raw.get("liquidation_price")
        positions.append(
            RISExPortfolioPosition(
                market_id=_positive_int(raw.get("market_id"), field="position market_id"),
                size=_decimal(
                    raw.get("size"),
                    field="position size",
                    allow_zero=True,
                    allow_negative=True,
                ),
                mark_price=_decimal(raw.get("mark_price"), field="position mark_price"),
                liquidation_price=(
                    None
                    if liquidation in (None, "")
                    else _decimal(
                        liquidation,
                        field="position liquidation_price",
                        allow_zero=True,
                    )
                ),
            )
        )

    return RISExPortfolioSnapshot(
        usdc_balance=_decimal(
            summary.get("usdc_balance"),
            field="usdc_balance",
            allow_zero=True,
        ),
        total_account_value=_decimal(
            summary.get("total_account_value"),
            field="total_account_value",
            allow_zero=True,
        ),
        total_notional=_decimal(
            summary.get("total_notional"),
            field="total_notional",
            allow_zero=True,
        ),
        free_collateral=_optional_nonnegative_decimal(
            summary.get("free_collateral"),
            field="free_collateral",
        ),
        in_liquidation=in_liquidation,
        risk_level=risk_level.strip().upper(),
        positions=tuple(positions),
    )


def _position_for_market(
    portfolio: RISExPortfolioSnapshot,
    market_id: int,
) -> RISExPortfolioPosition | None:
    matches = [position for position in portfolio.positions if position.market_id == market_id]
    if len(matches) > 1:
        raise RISExRiskPlanningDenied("RISEx portfolio has duplicate market positions")
    return matches[0] if matches else None


def build_risex_risk_context(
    *,
    portfolio: RISExPortfolioSnapshot,
    market: RISExMarketMetadata,
    effective_risk: EffectiveRisk,
    runtime: RISExRuntimeRiskFlags,
    close_only: bool = False,
    reducing: bool = False,
) -> RiskContext:
    if market.max_leverage is None or market.mark_price is None:
        raise RISExRiskPlanningDenied("RISEx market risk metadata is incomplete")

    current = _position_for_market(portfolio, market.market_id)
    current_size = current.size if current is not None else Decimal(0)
    current_asset_exposure = (
        abs(current_size) * current.mark_price if current is not None else Decimal(0)
    )

    free_margin = portfolio.free_collateral
    if free_margin is None:
        if not reducing:
            raise RISExRiskPlanningDenied(
                "RISEx free_collateral/free margin is indeterminate"
            )
        # Reduction paths do not consume margin headroom in evaluate(). Zero is
        # only a conservative computational sentinel, never reconstructed
        # provider truth.
        free_margin = Decimal(0)

    account_equity = portfolio.total_account_value
    leverage = (
        portfolio.total_notional / account_equity
        if account_equity > 0
        else Decimal(999)
    )
    provider_risk_alarm = portfolio.in_liquidation or portfolio.risk_level != "NORMAL"
    executable_minimum = market.min_order_size * market.mark_price

    return RiskContext(
        user_active=runtime.user_active,
        entitlement_active=runtime.entitlement_active,
        credential_active=runtime.credential_active,
        user_paused=runtime.user_paused,
        global_pause=runtime.global_pause,
        emergency_stop=runtime.emergency_stop,
        close_only=close_only,
        asset_allowed=runtime.asset_allowed,
        drawdown_halt=runtime.drawdown_halt,
        daily_loss_halt=runtime.daily_loss_halt,
        near_liquidation=runtime.near_liquidation or provider_risk_alarm,
        data_stale=runtime.data_stale,
        current_total_exposure=portfolio.total_notional,
        current_asset_exposure=current_asset_exposure,
        free_margin=free_margin,
        account_equity=account_equity,
        current_leverage=leverage,
        open_positions=sum(1 for position in portfolio.positions if position.size != 0),
        is_new_market=current_size == 0,
        max_notional_per_trade=effective_risk.max_notional_per_trade,
        max_total_exposure=effective_risk.max_total_exposure,
        max_asset_exposure=effective_risk.max_asset_exposure,
        max_leverage=effective_risk.max_leverage,
        max_positions=effective_risk.max_positions,
        minimum_executable_notional=executable_minimum,
    )


def floor_to_step(size: Decimal, step_size: Decimal) -> Decimal:
    if not size.is_finite() or not step_size.is_finite() or size <= 0 or step_size <= 0:
        raise RISExRiskPlanningDenied("RISEx size and step_size must be positive")
    steps = (size / step_size).to_integral_value(rounding=ROUND_FLOOR)
    return steps * step_size


def finalize_risex_authorized_size(
    size: Decimal,
    *,
    market: RISExMarketMetadata,
) -> Decimal:
    floored = floor_to_step(size, market.step_size)
    if floored <= 0:
        raise RISExRiskPlanningDenied("RISEx authorized size floors to zero")
    if floored < market.min_order_size:
        raise RISExRiskPlanningDenied(
            "RISEx risk-authorized size is below min_order_size; upward rounding is forbidden"
        )
    return floored


def provider_position_limit_non_binding_under_risk_caps(
    *,
    market: RISExMarketMetadata,
    account_equity: Decimal,
    effective_risk: EffectiveRisk,
) -> RISExProviderPositionLimitAssessment:
    if market.mark_price is None or market.mark_price <= 0:
        raise RISExRiskPlanningDenied("RISEx market mark price is indeterminate")
    absolute_risk_notional = min(
        effective_risk.max_asset_exposure,
        effective_risk.max_total_exposure,
        max(account_equity, Decimal(0)) * effective_risk.max_leverage,
    )
    risk_max_position_base = absolute_risk_notional / market.mark_price

    # RISEx has not yet documented the unit semantics of max_position_size.
    # Treat raw * step_size as the most conservative plausible base-asset
    # interpretation. We do not use it as an ordinary execution gate; we only
    # prove that every position the Risk Engine can authorize is strictly below
    # this lower-bound interpretation. If that proof fails, new exposure is
    # denied until provider semantics are verified.
    provider_limit = (
        None
        if market.max_position_size_raw is None
        else market.max_position_size_raw * market.step_size
    )
    non_binding = provider_limit is not None and risk_max_position_base < provider_limit
    return RISExProviderPositionLimitAssessment(
        provider_limit_base_conservative=provider_limit,
        risk_max_position_base=risk_max_position_base,
        non_binding=non_binding,
        unit_semantics_verified=False,
    )


def _job_decimal(context: Mapping[str, Any], key: str, *, positive: bool = False) -> Decimal:
    value = _decimal(
        context.get(key),
        field=f"CopyJob {key}",
        allow_zero=not positive,
        allow_negative=key == "master_position",
    )
    if positive and value <= 0:
        raise RISExRiskPlanningDenied(f"CopyJob {key} must be positive")
    return value


def _size_decimals(step_size: Decimal) -> int:
    exponent = step_size.normalize().as_tuple().exponent
    return max(-int(exponent), 0)


def _is_exposure_increasing(sizing: SizingResult) -> bool:
    return sizing.intent is OrderIntent.OPEN or (
        sizing.intent is OrderIntent.REVERSE and sizing.secondary is not None
    )


def _suppress_reopen(sizing: SizingResult, reason: str) -> SizingResult:
    if sizing.intent is OrderIntent.REVERSE and sizing.secondary is not None:
        return replace(sizing, secondary=None, notes=[*sizing.notes, reason])
    return sizing


def plan_risex_order_intent(
    *,
    job: object,
    portfolio_payload: Mapping[str, Any],
    markets_payload: Mapping[str, Any],
    risk: object,
    entitlement_data: Mapping[str, Any] | None,
    runtime: RISExRuntimeRiskFlags,
    client_order_id: int,
) -> RISExOrderIntent:
    symbol = str(getattr(job, "asset", "") or "").strip().upper()
    if not symbol:
        raise RISExRiskPlanningDenied("CopyJob asset is missing")
    context = getattr(job, "context", None)
    if not isinstance(context, Mapping):
        raise RISExRiskPlanningDenied("CopyJob context is missing")

    market = parse_risex_risk_market(markets_payload, symbol=symbol)
    if not market.active:
        raise RISExRiskPlanningDenied("RISEx market is inactive")
    if not market.unlocked:
        raise RISExRiskPlanningDenied("RISEx market is locked")
    if market.max_leverage is None or market.mark_price is None:
        raise RISExRiskPlanningDenied("RISEx market risk metadata is incomplete")

    portfolio = parse_risex_portfolio_details(portfolio_payload)
    current_position = _position_for_market(portfolio, market.market_id)
    current_size = current_position.size if current_position is not None else Decimal(0)

    effective_risk = resolve_effective_risk(
        risk,
        dict(entitlement_data or {}),
        exchange_max_leverage=market.max_leverage,
    )
    master_position = _job_decimal(context, "master_position")
    master_equity = _job_decimal(context, "master_equity", positive=True)
    master_mark = _job_decimal(context, "master_mark_price", positive=True)

    spec = AssetSpec(
        name=symbol,
        sz_decimals=_size_decimals(market.step_size),
        max_leverage=max(1, int(effective_risk.max_leverage)),
        min_notional=market.min_order_size * market.mark_price,
    )
    sizing = plan(
        MasterExposure(symbol, master_position, master_mark, master_equity),
        FollowerState(
            str(getattr(job, "id", uuid.UUID(int=0))),
            portfolio.total_account_value,
            Decimal(0),
            current_size,
            effective_risk.multiplier,
        ),
        spec,
        min_notional=Decimal(str(getattr(risk, "min_notional"))),
        follower_mark_price=market.mark_price,
    )
    if not sizing.actionable:
        raise RISExRiskPlanningDenied(sizing.reason or "RISEx sizing is not actionable")

    if market.reduce_only and _is_exposure_increasing(sizing):
        if sizing.intent is OrderIntent.REVERSE:
            sizing = _suppress_reopen(
                sizing,
                "RISEx provider reduce-only market suppressed reversal reopen",
            )
        else:
            raise RISExRiskPlanningDenied(
                "RISEx market is reduce-only; exposure-increasing intent denied"
            )

    provider_risk_alarm = portfolio.in_liquidation or portfolio.risk_level != "NORMAL"
    if provider_risk_alarm and _is_exposure_increasing(sizing):
        if sizing.intent is OrderIntent.REVERSE:
            sizing = _suppress_reopen(
                sizing,
                "RISEx provider risk alarm suppressed reversal reopen",
            )
        else:
            reason = (
                "RISEx provider risk: in_liquidation=true"
                if portfolio.in_liquidation
                else f"RISEx provider risk_level={portfolio.risk_level!r} is not NORMAL"
            )
            raise RISExRiskPlanningDenied(reason)

    position_limit = provider_position_limit_non_binding_under_risk_caps(
        market=market,
        account_equity=portfolio.total_account_value,
        effective_risk=effective_risk,
    )
    if _is_exposure_increasing(sizing) and not position_limit.non_binding:
        if sizing.intent is OrderIntent.REVERSE:
            sizing = _suppress_reopen(
                sizing,
                "RISEx max_position_size unit is unverified; reversal reopen suppressed",
            )
        else:
            raise RISExRiskPlanningDenied(
                "RISEx max_position_size unit is unverified and the conservative "
                "position limit could bind the Risk Engine"
            )

    reducing = sizing.intent in {OrderIntent.REDUCE, OrderIntent.CLOSE} or sizing.reduce_only
    effective_runtime = replace(
        runtime,
        entitlement_active=bool((entitlement_data or {}).get("entitled", True)),
    )
    risk_context = build_risex_risk_context(
        portfolio=portfolio,
        market=market,
        effective_risk=effective_risk,
        runtime=effective_runtime,
        close_only=bool(getattr(risk, "close_only")),
        reducing=reducing,
    )
    decision = evaluate(sizing, risk_context)
    if decision.action in {RiskAction.DENY, RiskAction.SKIP}:
        raise RISExRiskPlanningDenied(
            decision.reason or f"RISEx Risk Engine returned {decision.action.value}"
        )

    authorized_size = finalize_risex_authorized_size(
        decision.plan.order_size,
        market=market,
    )
    if authorized_size > decision.plan.order_size:
        raise RISExRiskPlanningDenied("RISEx authorized size may never round upward")

    if type(client_order_id) is not int or not 0 < client_order_id < (1 << 64):
        raise RISExRiskPlanningDenied("RISEx client_order_id must be a non-zero uint64")

    return RISExOrderIntent(
        symbol=symbol,
        is_buy=decision.plan.is_buy,
        requested_size=authorized_size,
        reduce_only=decision.plan.reduce_only,
        slippage_bps=int(getattr(risk, "max_slippage_bps")),
        client_order_id=client_order_id,
    )
