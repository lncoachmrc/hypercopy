from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.adapters.ratelimit import Priority
from app.engine.sizing import round_price
from app.services.ai_profit_exit_economics import (
    ProfitExitEconomicsInput,
    ProfitExitEconomicsResult,
    ProfitExitFill,
    ProfitExitFunding,
    evaluate_profit_exit_economics,
)


_MAX_FILLS_BY_TIME = 2000
_MAX_TIME_RANGE_ROWS = 500


@dataclass(frozen=True, slots=True)
class ProfitExitObservation:
    asset: str
    complete: bool
    eligible: bool
    current_position: Decimal | None
    entry_price: Decimal | None
    mark_price: Decimal | None
    executable_exit_price: Decimal | None
    taker_fee_rate: Decimal | None
    economics: ProfitExitEconomicsResult | None
    reason: str


def _fail(
    asset: str,
    reason: str,
    *,
    current_position: Decimal | None = None,
    entry_price: Decimal | None = None,
    mark_price: Decimal | None = None,
    executable_exit_price: Decimal | None = None,
    taker_fee_rate: Decimal | None = None,
    economics: ProfitExitEconomicsResult | None = None,
) -> ProfitExitObservation:
    return ProfitExitObservation(
        asset=asset,
        complete=False,
        eligible=False,
        current_position=current_position,
        entry_price=entry_price,
        mark_price=mark_price,
        executable_exit_price=executable_exit_price,
        taker_fee_rate=taker_fee_rate,
        economics=economics,
        reason=reason,
    )


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _position_row(
    state: dict,
    asset: str,
) -> dict | None:
    rows = state.get("assetPositions")
    if not isinstance(rows, list):
        return None

    wanted = asset.upper()
    for row in rows:
        if not isinstance(row, dict):
            continue
        position = row.get("position", row)
        if not isinstance(position, dict):
            continue
        if str(position.get("coin") or "").upper() == wanted:
            return position

    return None


def _parse_fill(
    row: dict,
) -> ProfitExitFill | None:
    time_ms = row.get("time")
    start = _decimal(row.get("startPosition"))
    size = _decimal(row.get("sz"))
    fee = _decimal(row.get("fee"))
    side = str(row.get("side") or "").upper()

    if (
        not isinstance(time_ms, int)
        or time_ms < 0
        or start is None
        or size is None
        or size <= 0
        or fee is None
        or fee < 0
        or side not in {"A", "B"}
    ):
        return None

    return ProfitExitFill(
        time_ms=time_ms,
        start_position=start,
        side=side,
        size=size,
        fee=fee,
    )


def _parse_funding(
    row: dict,
) -> ProfitExitFunding | None:
    time_ms = row.get("time")
    delta = row.get("delta")

    if not isinstance(time_ms, int) or time_ms < 0:
        return None
    if not isinstance(delta, dict):
        return None

    position = _decimal(delta.get("szi"))
    usdc = _decimal(delta.get("usdc"))

    if position is None or usdc is None:
        return None

    return ProfitExitFunding(
        time_ms=time_ms,
        position_size=position,
        usdc=usdc,
    )


async def collect_profit_exit_economics(
    hl,
    *,
    account_address: str,
    asset: str,
    history_start_ms: int,
    history_end_ms: int | None,
    slippage_bps: int,
) -> ProfitExitObservation:
    """
    Build a fail-closed economic snapshot for an AI profit exit.

    This function is read-only. It never creates CopyJobs, Executions or
    provider writes.
    """

    normalized_asset = str(asset).upper()

    if (
        not isinstance(history_start_ms, int)
        or history_start_ms < 0
        or (
            history_end_ms is not None
            and (
                not isinstance(history_end_ms, int)
                or history_end_ms < history_start_ms
            )
        )
    ):
        return _fail(
            normalized_asset,
            "Invalid profit-exit history window",
        )

    if (
        isinstance(slippage_bps, bool)
        or not isinstance(slippage_bps, int)
        or slippage_bps < 0
        or slippage_bps >= 10_000
    ):
        return _fail(
            normalized_asset,
            "Invalid profit-exit slippage",
        )

    try:
        snapshot = await hl.account_snapshot(
            account_address,
            priority=Priority.RECONCILE,
        )
    except Exception as exc:
        return _fail(
            normalized_asset,
            f"Follower account snapshot unavailable: {type(exc).__name__}",
        )

    perp_state = getattr(snapshot, "perp_state", None)
    if not isinstance(perp_state, dict):
        return _fail(
            normalized_asset,
            "Follower perpetual state is unavailable",
        )

    position = _position_row(
        perp_state,
        normalized_asset,
    )
    if position is None:
        return _fail(
            normalized_asset,
            "Follower position is flat or unavailable",
        )

    current_position = _decimal(position.get("szi"))
    if current_position is None:
        return _fail(
            normalized_asset,
            "Follower position size is invalid",
        )

    if current_position == 0:
        return _fail(
            normalized_asset,
            "Follower position is flat",
            current_position=current_position,
        )

    entry_price = _decimal(position.get("entryPx"))
    if entry_price is None or entry_price <= 0:
        return _fail(
            normalized_asset,
            "Follower entry price is unavailable",
            current_position=current_position,
        )

    try:
        mids = await hl.mids(
            priority=Priority.RECONCILE,
        )
    except Exception as exc:
        return _fail(
            normalized_asset,
            f"Follower market data unavailable: {type(exc).__name__}",
            current_position=current_position,
            entry_price=entry_price,
        )

    if not isinstance(mids, dict):
        return _fail(
            normalized_asset,
            "Follower market data is malformed",
            current_position=current_position,
            entry_price=entry_price,
        )

    mark_price = _decimal(mids.get(normalized_asset))
    if mark_price is None or mark_price <= 0:
        return _fail(
            normalized_asset,
            "Follower market price is unavailable",
            current_position=current_position,
            entry_price=entry_price,
        )

    try:
        spec = await hl.asset_spec(normalized_asset)
    except Exception as exc:
        return _fail(
            normalized_asset,
            f"Follower asset specification unavailable: {type(exc).__name__}",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
        )

    try:
        sz_decimals = int(spec.sz_decimals)
    except (AttributeError, TypeError, ValueError):
        return _fail(
            normalized_asset,
            "Follower asset specification is malformed",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
        )

    slip = Decimal(slippage_bps) / Decimal(10_000)

    # Long close = SELL => worst executable economics is the IOC limit below
    # mark. Short close = BUY => worst executable economics is the limit above
    # mark. Use exactly the same round_price() routine as place_ioc().
    aggressive = (
        mark_price * (Decimal(1) - slip)
        if current_position > 0
        else mark_price * (Decimal(1) + slip)
    )

    try:
        executable_exit_price = round_price(
            aggressive,
            sz_decimals,
        )
    except Exception as exc:
        return _fail(
            normalized_asset,
            f"Executable exit price unavailable: {type(exc).__name__}",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
        )

    if executable_exit_price <= 0:
        return _fail(
            normalized_asset,
            "Executable exit price is invalid",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
        )

    try:
        raw_fills = await hl.user_fills_by_time(
            account_address,
            history_start_ms,
            history_end_ms,
        )
    except Exception as exc:
        return _fail(
            normalized_asset,
            f"Profit-exit fill history unavailable: {type(exc).__name__}",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
            executable_exit_price=executable_exit_price,
        )

    if not isinstance(raw_fills, list):
        return _fail(
            normalized_asset,
            "Follower fill history is malformed",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
            executable_exit_price=executable_exit_price,
        )

    # Hyperliquid caps this endpoint at 2000 rows. A full page cannot prove the
    # entire position history, so fail closed immediately and do not spend more
    # reconciliation budget on funding/fees for an unusable observation.
    if len(raw_fills) >= _MAX_FILLS_BY_TIME:
        return _fail(
            normalized_asset,
            "Follower fill history may be truncated",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
            executable_exit_price=executable_exit_price,
        )

    try:
        raw_funding = await hl.user_funding_history(
            account_address,
            history_start_ms,
            history_end_ms,
        )
    except Exception as exc:
        return _fail(
            normalized_asset,
            f"Profit-exit funding history unavailable: {type(exc).__name__}",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
            executable_exit_price=executable_exit_price,
        )

    if not isinstance(raw_funding, list):
        return _fail(
            normalized_asset,
            "Follower funding history is malformed",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
            executable_exit_price=executable_exit_price,
        )

    # Same fail-closed rule for the bounded 500-row funding window.
    if len(raw_funding) >= _MAX_TIME_RANGE_ROWS:
        return _fail(
            normalized_asset,
            "Follower funding history may be truncated",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
            executable_exit_price=executable_exit_price,
        )

    try:
        raw_fees = await hl.user_fees(
            account_address,
        )
    except Exception as exc:
        return _fail(
            normalized_asset,
            f"Profit-exit fee schedule unavailable: {type(exc).__name__}",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
            executable_exit_price=executable_exit_price,
        )

    if not isinstance(raw_fees, dict):
        return _fail(
            normalized_asset,
            "Follower fee schedule is malformed",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
            executable_exit_price=executable_exit_price,
        )

    taker_fee_rate = _decimal(
        raw_fees.get("userCrossRate")
    )
    if taker_fee_rate is None or taker_fee_rate < 0:
        return _fail(
            normalized_asset,
            "Follower taker fee rate is unavailable",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
            executable_exit_price=executable_exit_price,
        )

    fills: list[ProfitExitFill] = []
    for row in raw_fills:
        if not isinstance(row, dict):
            return _fail(
                normalized_asset,
                "Follower fill history is malformed",
                current_position=current_position,
                entry_price=entry_price,
                mark_price=mark_price,
                executable_exit_price=executable_exit_price,
                taker_fee_rate=taker_fee_rate,
            )

        coin = str(row.get("coin") or "").upper()
        if coin != normalized_asset:
            continue

        parsed = _parse_fill(row)
        if parsed is None:
            return _fail(
                normalized_asset,
                "Follower asset fill history is incomplete",
                current_position=current_position,
                entry_price=entry_price,
                mark_price=mark_price,
                executable_exit_price=executable_exit_price,
                taker_fee_rate=taker_fee_rate,
            )

        fills.append(parsed)

    funding: list[ProfitExitFunding] = []
    for row in raw_funding:
        if not isinstance(row, dict):
            return _fail(
                normalized_asset,
                "Follower funding history is malformed",
                current_position=current_position,
                entry_price=entry_price,
                mark_price=mark_price,
                executable_exit_price=executable_exit_price,
                taker_fee_rate=taker_fee_rate,
            )

        delta = row.get("delta")
        if not isinstance(delta, dict):
            return _fail(
                normalized_asset,
                "Follower funding history is malformed",
                current_position=current_position,
                entry_price=entry_price,
                mark_price=mark_price,
                executable_exit_price=executable_exit_price,
                taker_fee_rate=taker_fee_rate,
            )

        coin = str(delta.get("coin") or "").upper()
        if coin != normalized_asset:
            continue

        parsed = _parse_funding(row)
        if parsed is None:
            return _fail(
                normalized_asset,
                "Follower asset funding history is incomplete",
                current_position=current_position,
                entry_price=entry_price,
                mark_price=mark_price,
                executable_exit_price=executable_exit_price,
                taker_fee_rate=taker_fee_rate,
            )

        funding.append(parsed)

    economics = evaluate_profit_exit_economics(
        ProfitExitEconomicsInput(
            current_position=current_position,
            entry_price=entry_price,
            executable_exit_price=executable_exit_price,
            taker_fee_rate=taker_fee_rate,
            fills=fills,
            funding=funding,
        )
    )

    if not economics.complete:
        return _fail(
            normalized_asset,
            economics.reason
            or "Profit-exit economics are incomplete",
            current_position=current_position,
            entry_price=entry_price,
            mark_price=mark_price,
            executable_exit_price=executable_exit_price,
            taker_fee_rate=taker_fee_rate,
            economics=economics,
        )

    return ProfitExitObservation(
        asset=normalized_asset,
        complete=True,
        eligible=economics.eligible,
        current_position=current_position,
        entry_price=entry_price,
        mark_price=mark_price,
        executable_exit_price=executable_exit_price,
        taker_fee_rate=taker_fee_rate,
        economics=economics,
        reason=(
            ""
            if economics.eligible
            else (
                economics.reason
                or "Residual net PnL is not strictly positive"
            )
        ),
    )
