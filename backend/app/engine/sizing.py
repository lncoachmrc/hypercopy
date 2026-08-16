"""Position targeting: where the follower should be, minus where they are.

This module replaces the trade-replication model used by every reference
implementation reviewed. The difference is not stylistic.

Trade replication mirrors each master *fill*. It works until the first partial
fill, the first margin rejection, or the first dropped websocket message -- at
which point the follower carries a difference that is never recovered, because
nothing in the system is looking at position state. After a month the follower's
book has no defined relationship to the master's, and nobody notices, because no
component's job is to notice.

Position targeting computes where the follower *should* be from the master's
current exposure, subtracts where they actually are, and sends the difference.
Applying it ten times running produces the same result as applying it once: after
the first pass the delta is zero. That idempotence is what makes reconciliation
possible and retries safe.

Everything here is pure. No network, no database, no clock. That is deliberate:
a long-to-short reversal or a drawdown breach can be tested without standing up
PostgreSQL or reaching Hyperliquid.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import Enum

# VERIFIED (Hyperliquid docs, exchange endpoint error responses):
#   "Order must have minimum value of $10."
EXCHANGE_MIN_NOTIONAL = Decimal("10")

# VERIFIED (Hyperliquid docs, meta.universe): perp prices carry at most
# 5 significant figures and at most (6 - szDecimals) decimal places.
MAX_PRICE_SIG_FIGS = 5
PERP_MAX_PRICE_DECIMALS = 6


class OrderIntent(str, Enum):
    """What a delta means in terms of exposure, not direction."""

    OPEN = "open"          # increases exposure in the master's direction
    REDUCE = "reduce"      # decreases exposure, reduce-only
    CLOSE = "close"        # takes the position to exactly zero
    REVERSE = "reverse"    # crosses zero: must be split into two orders
    NONE = "none"          # already on target


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """Exchange constraints for one perpetual market, read from `meta`."""

    name: str
    sz_decimals: int
    max_leverage: int
    only_isolated: bool = False


@dataclass(frozen=True, slots=True)
class MasterExposure:
    """The master's committed exposure on one asset, at a point in time."""

    asset: str
    position_size: Decimal        # signed: positive long, negative short
    mark_price: Decimal
    eligible_equity: Decimal

    @property
    def notional(self) -> Decimal:
        return abs(self.position_size) * self.mark_price

    @property
    def exposure_ratio(self) -> Decimal:
        """Fraction of the master's account committed to this asset.

        This is the quantity that travels to followers. Nobody's exposure
        depends on how large the master's account happens to be.
        """
        if self.eligible_equity <= 0:
            return Decimal(0)
        signed = Decimal(1) if self.position_size >= 0 else Decimal(-1)
        return signed * self.notional / self.eligible_equity


@dataclass(frozen=True, slots=True)
class FollowerState:
    """What we believe about a follower, from the position ledger."""

    user_id: str
    account_value: Decimal
    margin_used_unmanaged: Decimal = Decimal(0)
    current_size: Decimal = Decimal(0)     # signed, this asset
    multiplier: Decimal = Decimal(1)

    @property
    def eligible_equity(self) -> Decimal:
        """Account value minus margin locked in positions we do not manage.

        A follower holding half their account in a hand-opened trade does not
        want that half in the denominator. Using raw accountValue would size
        every copied trade against capital that is not actually available.
        """
        return max(self.account_value - self.margin_used_unmanaged, Decimal(0))


@dataclass(slots=True)
class SizingResult:
    asset: str
    intent: OrderIntent
    target_size: Decimal
    current_size: Decimal
    delta: Decimal
    order_size: Decimal            # rounded, absolute
    is_buy: bool
    reduce_only: bool
    notional: Decimal
    reason: str | None = None
    # A reversal needs two orders; the second is described here.
    secondary: "SizingResult | None" = None
    notes: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.order_size > 0 and self.intent is not OrderIntent.NONE


def round_size(size: Decimal, sz_decimals: int) -> Decimal:
    """Truncate toward zero to the asset's precision.

    Rounding *down* rather than to nearest is a safety property: rounding up on
    a close could push past the position and leave the follower holding
    unintended opposite exposure.
    """
    quantum = Decimal(1).scaleb(-sz_decimals)
    return abs(size).quantize(quantum, rounding=ROUND_DOWN)


def round_price(price: Decimal, sz_decimals: int) -> Decimal:
    """Apply Hyperliquid's tick rules so the order is not rejected on format."""
    if price <= 0:
        return price
    max_places = max(PERP_MAX_PRICE_DECIMALS - sz_decimals, 0)
    exponent = price.adjusted()
    sig_quantum = Decimal(1).scaleb(exponent - (MAX_PRICE_SIG_FIGS - 1))
    stepped = price.quantize(sig_quantum, rounding=ROUND_HALF_UP)
    return stepped.quantize(Decimal(1).scaleb(-max_places), rounding=ROUND_HALF_UP)


def compute_target(
    master: MasterExposure,
    follower: FollowerState,
) -> Decimal:
    """Signed position size the follower should hold on this asset.

        target_notional = exposure_ratio x eligible_equity x multiplier
        target_size     = target_notional / mark_price
    """
    if master.mark_price <= 0 or follower.eligible_equity <= 0:
        return Decimal(0)
    target_notional = (
        master.exposure_ratio * follower.eligible_equity * follower.multiplier
    )
    return target_notional / master.mark_price


def classify(target: Decimal, current: Decimal) -> OrderIntent:
    """Decide what kind of move the delta represents."""
    if target == current:
        return OrderIntent.NONE
    if current == 0:
        return OrderIntent.OPEN
    if target == 0:
        return OrderIntent.CLOSE
    # Sign change means the order would have to cross zero.
    if (target > 0) != (current > 0):
        return OrderIntent.REVERSE
    return OrderIntent.OPEN if abs(target) > abs(current) else OrderIntent.REDUCE


def plan(
    master: MasterExposure,
    follower: FollowerState,
    spec: AssetSpec,
    *,
    min_notional: Decimal = EXCHANGE_MIN_NOTIONAL,
) -> SizingResult:
    """Turn master exposure and follower state into an executable order plan.

    A delta below the exchange minimum is reported as NONE but the target is
    left untouched, so the shortfall accumulates and clears on a later event.
    Discarding it would reintroduce exactly the drift this module exists to
    remove.
    """
    target = compute_target(master, follower)
    current = follower.current_size
    delta = target - current
    intent = classify(target, current)
    price = master.mark_price

    if intent is OrderIntent.NONE:
        return SizingResult(
            asset=master.asset,
            intent=intent,
            target_size=target,
            current_size=current,
            delta=Decimal(0),
            order_size=Decimal(0),
            is_buy=False,
            reduce_only=False,
            notional=Decimal(0),
            reason="Already on target",
        )

    if intent is OrderIntent.REVERSE:
        return _plan_reversal(master, follower, spec, target, current, min_notional)

    order_size = round_size(delta, spec.sz_decimals)
    notional = order_size * price
    reduce_only = intent in (OrderIntent.REDUCE, OrderIntent.CLOSE)

    # Never sell more than is held. Guards against a stale ledger producing a
    # reduce-only order the exchange would reject outright.
    if reduce_only and order_size > abs(current):
        order_size = round_size(abs(current), spec.sz_decimals)
        notional = order_size * price

    floor = max(min_notional, EXCHANGE_MIN_NOTIONAL)
    if order_size <= 0:
        return SizingResult(
            asset=master.asset, intent=OrderIntent.NONE, target_size=target,
            current_size=current, delta=delta, order_size=Decimal(0),
            is_buy=delta > 0, reduce_only=reduce_only, notional=Decimal(0),
            reason=f"Delta rounds to zero at {spec.sz_decimals} decimals",
        )

    if notional < floor:
        return SizingResult(
            asset=master.asset, intent=OrderIntent.NONE, target_size=target,
            current_size=current, delta=delta, order_size=Decimal(0),
            is_buy=delta > 0, reduce_only=reduce_only, notional=notional,
            reason=(
                f"${notional:.2f} is under the ${floor:.0f} minimum; "
                "the difference stays on target and clears on a later move"
            ),
        )

    return SizingResult(
        asset=master.asset,
        intent=intent,
        target_size=target,
        current_size=current,
        delta=delta,
        order_size=order_size,
        is_buy=delta > 0,
        reduce_only=reduce_only,
        notional=notional,
    )


def _plan_reversal(
    master: MasterExposure,
    follower: FollowerState,
    spec: AssetSpec,
    target: Decimal,
    current: Decimal,
    min_notional: Decimal,
) -> SizingResult:
    """Split a sign change into close-then-open.

    A single order crossing zero is unsafe in both available forms: marked
    reduce-only the exchange rejects the portion beyond flat, and not marked
    reduce-only a partial fill can leave unintended exposure on the far side.
    Two orders cost roughly 200ms more and cannot do either.
    """
    price = master.mark_price
    close_size = round_size(abs(current), spec.sz_decimals)
    open_size = round_size(abs(target), spec.sz_decimals)
    floor = max(min_notional, EXCHANGE_MIN_NOTIONAL)

    secondary: SizingResult | None = None
    if open_size > 0 and open_size * price >= floor:
        secondary = SizingResult(
            asset=master.asset,
            intent=OrderIntent.OPEN,
            target_size=target,
            current_size=Decimal(0),      # by the time it runs, we are flat
            delta=target,
            order_size=open_size,
            is_buy=target > 0,
            reduce_only=False,
            notional=open_size * price,
            notes=["Second leg of a reversal"],
        )

    return SizingResult(
        asset=master.asset,
        intent=OrderIntent.REVERSE,
        target_size=target,
        current_size=current,
        delta=target - current,
        order_size=close_size,
        is_buy=current < 0,               # buy to close a short
        reduce_only=True,
        notional=close_size * price,
        secondary=secondary,
        notes=["First leg: flatten before reopening on the other side"],
    )


def effective_leverage(
    positions_notional: Decimal, account_value: Decimal
) -> Decimal:
    """Leverage across the whole book, not one order in isolation."""
    if account_value <= 0:
        return Decimal(0)
    return positions_notional / account_value
