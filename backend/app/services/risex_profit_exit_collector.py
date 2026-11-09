"""Read-only RISEx TESTNET shadow economics. No signed requests or orders.

Entry follows the master, fees are an explicit operator assumption, funding is
unknown. Values are comparison estimates and NEVER authorize a live close.
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

RISEX_SHADOW_BASIS = 'risex_copy_shadow_estimate'


def _finite(raw) -> Decimal:
    if raw is None or isinstance(raw, bool):
        raise ValueError('Missing/invalid economic input')
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('Invalid decimal') from exc
    if not value.is_finite():
        raise ValueError('Nonfinite decimal')
    return value


def quote_risex_shadow(*, position, entry, bid, ask, tick, slippage_bps, fee_bps) -> dict:
    position, entry, bid, ask, tick, fee_bps = map(_finite, (position, entry, bid, ask, tick, fee_bps))
    if position == 0 or min(entry, bid, ask, tick) <= 0 or bid > ask:
        raise ValueError('Invalid position or market')
    if type(slippage_bps) is not int or not 0 <= slippage_bps < 10000:
        raise ValueError('Invalid slippage')
    if not 0 <= fee_bps < 10000:
        raise ValueError('Invalid configured fee assumption')
    rate, slip = fee_bps / 10000, Decimal(slippage_bps) / 10000
    raw_exit = bid * (1-slip) if position > 0 else ask * (1+slip)
    rounding = ROUND_FLOOR if position > 0 else ROUND_CEILING
    exit_price = (raw_exit / tick).to_integral_value(rounding=rounding) * tick
    if exit_price <= 0:
        raise ValueError('Rounded exit price is not positive')
    gross = position * (exit_price-entry)
    entry_fee, exit_fee = abs(position) * entry * rate, abs(position) * exit_price * rate
    return dict(position=position, entry=entry, exit_price=exit_price, book_mid=(bid+ask)/2,
                fee_rate=rate, gross=gross, entry_fee=entry_fee, exit_fee=exit_fee,
                net=gross-entry_fee-exit_fee)


async def collect_risex_shadow_profit_exit(*, api, network: str, asset: str,
                                           shadow_position: Decimal, master_perp_state: dict,
                                           slippage_bps: int):
    from app.services.ai_profit_exit_collector import ProfitExitObservation
    from app.services.ai_profit_exit_economics import ProfitExitEconomicsResult
    from app.services.risex_order_preparation import (
        assert_risex_environment_allowed, load_top_of_book, resolve_market_metadata,
    )

    position = entry = mark = exit_price = rate = None
    funding_model = 'not_modeled_fee_unknown'

    def failure(reason):
        return ProfitExitObservation(
            asset=asset, complete=False, eligible=False, current_position=position,
            entry_price=entry, mark_price=mark, executable_exit_price=exit_price,
            taker_fee_rate=rate, economics=None, reason=reason,
            basis=RISEX_SHADOW_BASIS, pnl_complete=False, funding_model=funding_model,
        )

    if network != 'testnet' or os.getenv('ENABLE_LIVE_TRADING') != 'false':
        return failure('RISEx shadow collector is restricted to the isolated testnet stack')
    if getattr(api, 'public_read_only', False) is not True:
        return failure('RISEx shadow requires a public read-only transport')
    try:
        assert_risex_environment_allowed(network='testnet', env=os.environ)
        position = _finite(shadow_position)
        rows = master_perp_state.get('assetPositions')
        if not isinstance(rows, list):
            raise ValueError('Master positions unavailable')
        matches = [r.get('position', r) for r in rows if isinstance(r, dict)
                   and isinstance(r.get('position', r), dict)
                   and str(r.get('position', r).get('coin', '')).upper() == asset.upper()]
        if len(matches) != 1:
            raise ValueError('Master asset is missing or ambiguous')
        source_size, entry = _finite(matches[0].get('szi')), _finite(matches[0].get('entryPx'))
        if position * source_size <= 0 or entry <= 0:
            raise ValueError('Master and target position disagree')
        # Required explicit assumption, not a fee read from the user account.
        fee = _finite(os.getenv('RISEX_SHADOW_TAKER_FEE_BPS'))
        if not 0 <= fee < 10000 or len(str(fee.normalize())) > 16:
            raise ValueError('Invalid fee assumption')
        funding_model = f'not_modeled_fee_{fee.normalize()}bps'
        market = await resolve_market_metadata(api, symbol=asset)
        book = await load_top_of_book(api, market_id=market.market_id)
        q = quote_risex_shadow(position=position, entry=entry, bid=book.best_bid,
                               ask=book.best_ask, tick=market.step_price,
                               slippage_bps=slippage_bps, fee_bps=fee)
        mark, exit_price, rate = q['book_mid'], q['exit_price'], q['fee_rate']
    except Exception as exc:
        # Never persist exception contents: a transport may contain credentials.
        return failure(f'RISEx shadow economics unavailable ({type(exc).__name__}); verify market and configured fees')

    economics = ProfitExitEconomicsResult(
        complete=True, eligible=q['net'] > 0, gross_price_pnl=q['gross'],
        residual_entry_fees=q['entry_fee'], residual_funding=None,
        estimated_exit_fee=q['exit_fee'], net_pnl=q['net'],
        reason='Estimated fees and master entry; funding not modeled',
    )
    return ProfitExitObservation(
        asset=asset, complete=True, eligible=economics.eligible, current_position=position,
        entry_price=entry, mark_price=mark, executable_exit_price=exit_price,
        taker_fee_rate=rate, economics=economics,
        reason='Configured fee assumption; master entry; funding not modeled',
        basis=RISEX_SHADOW_BASIS, pnl_complete=False, funding_model=funding_model,
    )
