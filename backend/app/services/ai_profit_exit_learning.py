"""Causal, research-only feedback for AI Profit Exit SHADOW decisions.

Never trains on a future observation relative to the decision being assessed.
Never writes model weights, orders, risk flags, or deployment settings.
Next-observation PnL is a proxy, NOT a verified counterfactual trade outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median

from sqlalchemy import and_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import AIProfitExitDecision, AIProfitExitObservation

LEARNING_VERSION = "shadow_next_observation_feedback_v1"
MAX_NEXT_OBSERVATION_SECONDS = 7200
MIN_INDEPENDENT_CYCLES = 20
MIN_LATER_CYCLES = 8


@dataclass(frozen=True, slots=True)
class ShadowLearningPair:
    cycle_id: str
    decision_at: datetime
    next_observation_at: datetime
    action: str
    decision_net_pnl: Decimal
    next_net_pnl: Decimal
    entry_notional: Decimal


def _finite(raw) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return val if val.is_finite() else None


def _normalize_pair(pair: ShadowLearningPair, now: datetime) -> Decimal | None:
    """Return signed immediate-vs-next valuation advantage in basis points."""
    if pair.action not in ("CLOSE_PROFIT", "HOLD"):
        return None
    if (
        pair.decision_at.tzinfo is None or pair.next_observation_at.tzinfo is None
        or pair.decision_at >= pair.next_observation_at
        or pair.next_observation_at > now
        or pair.next_observation_at - pair.decision_at > timedelta(seconds=MAX_NEXT_OBSERVATION_SECONDS)
    ):
        return None
    current = _finite(pair.decision_net_pnl)
    later = _finite(pair.next_net_pnl)
    notional = _finite(pair.entry_notional)
    if current is None or later is None or notional is None or current <= 0 or notional <= 0:
        return None
    delta = current - later if pair.action == "CLOSE_PROFIT" else later - current
    return delta / notional * Decimal("10000")


def learning_feedback_from_pairs(
    pairs: list[ShadowLearningPair], *, now: datetime,
    execution_provider: str, execution_network: str, asset: str,
) -> dict:
    """Aggregate one paired outcome per master source cycle.

    Results are descriptive short-horizon proxies, not trade returns.
    No cross-provider pooling, no automated policy promotion.
    """
    if now.tzinfo is None:
        raise ValueError("Learning feedback needs timezone-aware clock")
    ordered = sorted(pairs, key=lambda p: p.decision_at, reverse=True)
    by_cycle: dict[str, tuple[ShadowLearningPair, Decimal]] = {}
    for pair in ordered:
        if not pair.cycle_id or pair.cycle_id in by_cycle:
            continue
        advantage = _normalize_pair(pair, now)
        if advantage is None:
            continue
        by_cycle[pair.cycle_id] = (pair, advantage)
    samples = sorted(by_cycle.values(), key=lambda p: p[0].decision_at)
    n = len(samples)
    base = {
        "version": LEARNING_VERSION,
        "status": "collecting",
        "provider": execution_provider,
        "network": execution_network,
        "asset": asset,
        "side": "SHORT",
        "independent_source_cycles": n,
        "minimum_required_cycles": MIN_INDEPENDENT_CYCLES,
        "horizon": "next_observation_within_2h",
        "data_quality": "hypothetical_next_step_proxy_not_realized_trade_pnl",
        "policy_promotion": "disabled",
    }
    # Do not present a trend to the LLM from a handful of correlated fills.
    if n < MIN_INDEPENDENT_CYCLES:
        return base
    train_end = int(n * 0.7)
    later = samples[train_end:]
    if len(later) < MIN_LATER_CYCLES:
        return base
    stats = {}
    for action in ("CLOSE_PROFIT", "HOLD"):
        observed = [adv for pair, adv in later if pair.action == action]
        if observed:
            stats[action] = {
                "independent_cycles": len(observed),
                "favorable_next_observation": sum(v > 0 for v in observed),
                "unfavorable_next_observation": sum(v < 0 for v in observed),
                "median_signed_advantage_bps": str(median(observed).quantize(Decimal("0.01"))),
            }
    return {
        **base,
        "status": "exploratory",
        "earlier_cycles": train_end,
        "later_cycles": len(later),
        "later_period_observations": stats,
        "interpretation": (
            "Each decision is compared with the next verified shadow valuation, "
            "not with an actual alternative execution. These counts are not a "
            "causal performance claim or authorization to alter execution."
        ),
    }


def _learning_query(*, provider: str, network: str, asset: str, now: datetime):
    """One bounded read with a LATERAL next-observation lookup per prior decision."""
    d = AIProfitExitDecision
    o = AIProfitExitObservation
    later = (
        select(
            o.net_pnl.label("future_net"),
            o.observed_at.label("future_at"),
        ).where(
            o.user_id == d.user_id,
            o.execution_epoch_id == d.execution_epoch_id,
            o.execution_provider == d.execution_provider,
            o.execution_network == d.execution_network,
            o.asset == d.asset,
            o.source_cycle_id == d.source_cycle_id,
            o.state_version == d.state_version,
            o.follower_position_size == d.follower_position_size,
            o.pnl_complete == d.pnl_complete,
            o.complete.is_(True),
            o.position_matches.is_(True),
            o.net_pnl.is_not(None),
            o.observed_at > d.decided_at,
        ).order_by(o.observed_at.asc()).limit(1).lateral()
    )
    return (
        select(
            d.source_cycle_id,
            d.decided_at,
            d.action,
            d.net_pnl,
            d.follower_position_size,
            d.decision_inputs,
            later.c.future_net,
            later.c.future_at,
        )
        .select_from(d)
        .join(later, true())
        .where(
            d.execution_provider == provider,
            d.execution_network == network,
            d.asset == asset,
            d.side == "SHORT",
            d.intent_state.is_(None),
            d.net_pnl > 0,
            d.decided_at < now,
            d.decided_at >= now - timedelta(days=60),
        )
        .order_by(d.decided_at.desc(), d.id.desc())
        .limit(500)
    )


async def read_shadow_learning_feedback(
    db: AsyncSession, *, provider: str, network: str, asset: str,
    current_source_cycle_id: str, now: datetime,
) -> dict:
    """Collect historical paired SHADOW evidence, never ON or current cycle.

    Returns aggregate feedback; the LLM never sees other account identities.
    """
    if provider not in ("hyperliquid", "risex"):
        raise ValueError("Unsupported learning provider")
    if network not in ("testnet", "mainnet"):
        raise ValueError("Unsupported learning network")
    rows = (await db.execute(
        _learning_query(provider=provider, network=network, asset=asset, now=now)
    )).all()
    pairs = []
    for cycle_id, decided_at, action, net, size, inputs, future_net, future_at in rows:
        if cycle_id == current_source_cycle_id:
            continue
        if not isinstance(inputs, dict) or inputs.get("copy_state") != "SHADOW":
            continue
        if inputs.get("evaluation_basis") not in ("copy_shadow_estimate", "risex_shadow_estimate"):
            continue
        entry = _finite(inputs.get("entry_price"))
        quantity = _finite(size)
        current = _finite(net)
        future = _finite(future_net)
        if entry is None or entry <= 0 or quantity is None or quantity == 0 or current is None or future is None:
            continue
        pairs.append(ShadowLearningPair(
            cycle_id=cycle_id,
            decision_at=decided_at,
            next_observation_at=future_at,
            action=action,
            decision_net_pnl=current,
            next_net_pnl=future,
            entry_notional=abs(quantity) * entry,
        ))
    return learning_feedback_from_pairs(
        pairs, now=now, execution_provider=provider,
        execution_network=network, asset=asset,
    )
