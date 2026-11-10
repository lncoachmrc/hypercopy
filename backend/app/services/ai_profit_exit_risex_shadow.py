"""Read-only RISEx TESTNET shadow economics for follower Profit Exit research.

No signed REST/RPC calls, no live positions, no orders. Net PnL is a
hypothetical estimate using RISEx market quotes, a master entry-price proxy,
an explicitly modelled tier-1 taker fee and *no* funding.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from app.adapters.risex_types import RISExPublicReadTransport
from app.services.ai_profit_exit_collector import ProfitExitObservation, _position_row
from app.services.ai_profit_exit_economics import ProfitExitEconomicsResult
from app.services.risex_order_preparation import load_top_of_book, resolve_market_metadata

RISEX_SHADOW_BASIS = "risex_shadow_estimate"
# Research assumption: RISEx published base tier 0.03% taker; the actual
# account tier, builder fees, funding and fill price are NOT observed here.
RISEX_RESEARCH_TAKER_FEE_RATE = Decimal("0.0003")


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _incomplete(
    asset: str, reason: str, *, size: Decimal | None = None,
    entry: Decimal | None = None, mark: Decimal | None = None,
) -> ProfitExitObservation:
    return ProfitExitObservation(
        asset=asset, complete=False, eligible=False,
        current_position=size, entry_price=entry, mark_price=mark,
        executable_exit_price=None, taker_fee_rate=None,
        economics=None, reason=reason[:240], basis=RISEX_SHADOW_BASIS,
        pnl_complete=False, funding_model="not_modeled",
    )


async def collect_risex_shadow_profit_exit_economics(
    api: RISExPublicReadTransport,
    *,
    asset: str,
    shadow_position: Decimal,
    master_perp_state: dict,
    slippage_bps: int,
    network: str,
) -> ProfitExitObservation:
    """Estimate a hypothetical follower exit without any provider-side write.

    The actual RISEx user's position and cost basis are not assumed to match
    the master. This function must only be used for SHADOW research.
    """
    coin = str(asset).strip().upper()
    size = _decimal(shadow_position)
    if network != "testnet":
        return _incomplete(coin, "RISEx shadow observation requires testnet")
    if getattr(api, "public_read_only", False) is not True:
        return _incomplete(coin, "RISEx transport is not public read-only")
    if size is None or size == 0:
        return _incomplete(coin, "Shadow target is flat or invalid", size=size)
    if type(slippage_bps) is not int or not 0 <= slippage_bps < 10_000:
        return _incomplete(coin, "RISEx shadow slippage is invalid", size=size)
    row = _position_row(master_perp_state, coin) if isinstance(master_perp_state, dict) else None
    if row is None:
        return _incomplete(coin, "Verified Hyperliquid master entry is missing", size=size)
    master_size = _decimal(row.get("szi"))
    entry = _decimal(row.get("entryPx"))
    if (
        master_size is None or master_size == 0 or master_size * size <= 0
        or entry is None or entry <= 0
    ):
        return _incomplete(coin, "Master entry/position incompatible with shadow target", size=size, entry=entry)

    try:
        market = await resolve_market_metadata(api, symbol=coin)
        if market.mark_price is None or market.mark_price <= 0:
            return _incomplete(coin, "RISEx market mark unavailable", size=size, entry=entry)
        if not (market.step_size > 0 and market.step_price > 0):
            return _incomplete(coin, "RISEx market precision unavailable", size=size, entry=entry)
        if abs(size) < market.min_order_size or abs(size) % market.step_size != 0:
            return _incomplete(coin, "Shadow position cannot be closed at RISEx market steps", size=size, entry=entry, mark=market.mark_price)
        book = await load_top_of_book(api, market_id=market.market_id)
    except Exception as exc:
        return _incomplete(coin, f"RISEx testnet public market/book unavailable: {type(exc).__name__}", size=size, entry=entry)

    slip = Decimal(slippage_bps) / Decimal("10000")
    # A long exit sells against bid; a short exit buys against ask.
    raw_exit = book.best_bid * (1 - slip) if size > 0 else book.best_ask * (1 + slip)
    if raw_exit <= 0:
        return _incomplete(coin, "Invalid conservative exit price", size=size, entry=entry, mark=market.mark_price)
    ticks = (raw_exit / market.step_price).to_integral_value(
        rounding=ROUND_FLOOR if size > 0 else ROUND_CEILING,
    )
    exit_price = ticks * market.step_price
    if exit_price <= 0:
        return _incomplete(coin, "Invalid rounded exit price", size=size, entry=entry, mark=market.mark_price)
    quantity = abs(size)
    gross = (exit_price - entry) * size
    entry_fee = quantity * entry * RISEX_RESEARCH_TAKER_FEE_RATE
    exit_fee = quantity * exit_price * RISEX_RESEARCH_TAKER_FEE_RATE
    estimated_net = gross - entry_fee - exit_fee
    economics = ProfitExitEconomicsResult(
        complete=True, eligible=estimated_net > 0,
        gross_price_pnl=gross, residual_entry_fees=entry_fee,
        residual_funding=Decimal(0), estimated_exit_fee=exit_fee,
        net_pnl=estimated_net,
        reason=None if estimated_net > 0 else "RISEx hypothetical net PnL is not positive",
    )
    return ProfitExitObservation(
        asset=coin, complete=True, eligible=economics.eligible,
        current_position=size, entry_price=entry, mark_price=market.mark_price,
        executable_exit_price=exit_price, taker_fee_rate=RISEX_RESEARCH_TAKER_FEE_RATE,
        economics=economics, reason=economics.reason or "",
        basis=RISEX_SHADOW_BASIS, pnl_complete=False,
        funding_model="not_modeled",
    )
