from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True, slots=True)
class ProfitExitFill:
    time_ms: int
    start_position: Decimal
    side: str
    size: Decimal
    fee: Decimal


@dataclass(frozen=True, slots=True)
class ProfitExitFunding:
    time_ms: int
    position_size: Decimal
    usdc: Decimal


@dataclass(frozen=True, slots=True)
class ProfitExitEconomicsInput:
    current_position: Decimal
    entry_price: Decimal
    executable_exit_price: Decimal
    taker_fee_rate: Decimal
    fills: list[ProfitExitFill]
    funding: list[ProfitExitFunding]


@dataclass(frozen=True, slots=True)
class ProfitExitEconomicsResult:
    complete: bool
    eligible: bool
    gross_price_pnl: Decimal | None
    residual_entry_fees: Decimal | None
    residual_funding: Decimal | None
    estimated_exit_fee: Decimal | None
    net_pnl: Decimal | None
    reason: str | None = None


def _incomplete(reason: str) -> ProfitExitEconomicsResult:
    return ProfitExitEconomicsResult(
        complete=False,
        eligible=False,
        gross_price_pnl=None,
        residual_entry_fees=None,
        residual_funding=None,
        estimated_exit_fee=None,
        net_pnl=None,
        reason=reason,
    )


def _finite(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _fill_delta(fill: ProfitExitFill) -> Decimal | None:
    side = str(fill.side).upper()
    if side == "B":
        return fill.size
    if side == "A":
        return -fill.size
    return None


def _validate_fill(fill: ProfitExitFill) -> bool:
    return bool(
        isinstance(fill.time_ms, int)
        and fill.time_ms >= 0
        and _finite(fill.start_position)
        and _finite(fill.size)
        and fill.size > 0
        and _finite(fill.fee)
        and fill.fee >= 0
        and _fill_delta(fill) is not None
    )


def _validate_funding(item: ProfitExitFunding) -> bool:
    return bool(
        isinstance(item.time_ms, int)
        and item.time_ms >= 0
        and _finite(item.position_size)
        and _finite(item.usdc)
    )


def evaluate_profit_exit_economics(
    data: ProfitExitEconomicsInput,
) -> ProfitExitEconomicsResult:
    """
    Evaluate whether closing the authoritative residual position is still
    strictly profitable after residual entry fees, funding and estimated exit
    taker fee.

    The fill history is replayed from flat. Costs belonging to already-realized
    position portions are removed proportionally as exposure is reduced. A
    reversal starts a fresh residual cost cycle.

    Any incomplete or contradictory history fails closed.
    """

    current = data.current_position
    entry = data.entry_price
    exit_price = data.executable_exit_price
    fee_rate = data.taker_fee_rate

    if not _finite(current) or current == 0:
        return _incomplete("Current position is flat or invalid")
    if not _finite(entry) or entry <= 0:
        return _incomplete("Entry price is invalid")
    if not _finite(exit_price) or exit_price <= 0:
        return _incomplete("Executable exit price is invalid")
    if not _finite(fee_rate) or fee_rate < 0:
        return _incomplete("Taker fee rate is invalid")
    if not data.fills:
        return _incomplete("Fill history is unavailable")

    fills = sorted(data.fills, key=lambda item: item.time_ms)
    funding = sorted(data.funding, key=lambda item: item.time_ms)

    if any(not _validate_fill(fill) for fill in fills):
        return _incomplete("Fill history contains invalid values")
    if any(not _validate_funding(item) for item in funding):
        return _incomplete("Funding history contains invalid values")

    # Same-millisecond fill/funding ordering is not provable from these compact
    # inputs, so do not guess which position the funding observation belongs to.
    fill_times = {fill.time_ms for fill in fills}
    if any(item.time_ms in fill_times for item in funding):
        return _incomplete("Fill/funding event ordering is ambiguous")

    events: list[tuple[int, int, ProfitExitFill | ProfitExitFunding]] = [
        (fill.time_ms, 0, fill) for fill in fills
    ]
    events.extend((item.time_ms, 1, item) for item in funding)
    events.sort(key=lambda row: (row[0], row[1]))

    position = Decimal(0)
    residual_entry_fees = Decimal(0)
    residual_funding = Decimal(0)

    for _, kind, event in events:
        if kind == 1:
            assert isinstance(event, ProfitExitFunding)

            if event.position_size != position:
                return _incomplete(
                    "Funding observation does not match reconstructed position"
                )

            if position == 0:
                if event.usdc != 0:
                    return _incomplete(
                        "Non-zero funding was observed while reconstructed flat"
                    )
                continue

            residual_funding += event.usdc
            continue

        assert isinstance(event, ProfitExitFill)

        if event.start_position != position:
            return _incomplete(
                "Fill history does not begin at or continue from known position"
            )

        delta = _fill_delta(event)
        if delta is None:
            return _incomplete("Unsupported fill side")

        previous = position
        updated = previous + delta

        if previous == 0:
            # Fresh cycle opened from flat.
            residual_entry_fees = event.fee
            residual_funding = Decimal(0)

        elif updated == 0:
            # Cycle fully realized. No old cost may leak into the next cycle.
            residual_entry_fees = Decimal(0)
            residual_funding = Decimal(0)

        elif previous * updated > 0:
            previous_abs = abs(previous)
            updated_abs = abs(updated)

            if updated_abs > previous_abs:
                # Same-side increase: the whole new fill remains in exposure.
                residual_entry_fees += event.fee

            elif updated_abs < previous_abs:
                # Same-side reduction: only the proportional cost/funding pool
                # belonging to the surviving residual remains relevant.
                ratio = updated_abs / previous_abs
                residual_entry_fees *= ratio
                residual_funding *= ratio

            else:
                return _incomplete("Fill produced no position-size change")

        else:
            # Reversal. The opposing fill first closes the old cycle, then the
            # excess opens the new side. Allocate only that opening fraction of
            # the fill fee to the new residual.
            opening_size = abs(updated)
            if opening_size <= 0 or opening_size >= event.size:
                return _incomplete("Reversal fee allocation is not provable")

            residual_entry_fees = event.fee * opening_size / event.size
            residual_funding = Decimal(0)

        position = updated

    if position != current:
        return _incomplete(
            "Reconstructed fill position does not match authoritative current position"
        )

    try:
        if current > 0:
            gross = (exit_price - entry) * current
        else:
            gross = (entry - exit_price) * abs(current)

        estimated_exit_fee = abs(current) * exit_price * fee_rate
        net = (
            gross
            - residual_entry_fees
            + residual_funding
            - estimated_exit_fee
        )
    except (InvalidOperation, TypeError, ValueError):
        return _incomplete("Profit-exit economics could not be calculated")

    if any(
        not value.is_finite()
        for value in (
            gross,
            residual_entry_fees,
            residual_funding,
            estimated_exit_fee,
            net,
        )
    ):
        return _incomplete("Profit-exit economics produced non-finite values")

    return ProfitExitEconomicsResult(
        complete=True,
        eligible=net > 0,
        gross_price_pnl=gross,
        residual_entry_fees=residual_entry_fees,
        residual_funding=residual_funding,
        estimated_exit_fee=estimated_exit_fee,
        net_pnl=net,
        reason=None if net > 0 else "Residual net PnL is not strictly positive",
    )
