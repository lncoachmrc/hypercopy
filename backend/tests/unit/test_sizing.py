from decimal import Decimal

from app.engine.sizing import AssetSpec, FollowerState, MasterExposure, OrderIntent, plan, round_price

SPEC = AssetSpec('BTC', 5, 40)
D = Decimal


def make(master_size, current='0', equity='10000', master_equity='1000000', price='60000', multiplier='1'):
    return plan(
        MasterExposure('BTC', D(master_size), D(price), D(master_equity)),
        FollowerState('u', D(equity), D('0'), D(current), D(multiplier)),
        SPEC,
    )


def test_open_long_by_exposure_ratio():
    r = make('20')
    assert r.intent == OrderIntent.OPEN
    assert r.target_size == D('0.20000')
    assert r.order_size == D('0.20000')
    assert r.is_buy and not r.reduce_only


def test_increase_long():
    r = make('20', current='0.15')
    assert r.intent == OrderIntent.OPEN
    assert r.order_size == D('0.05000')


def test_reduce_long_is_reduce_only():
    r = make('12', current='0.20')
    assert r.intent == OrderIntent.REDUCE
    assert r.order_size == D('0.08000')
    assert not r.is_buy and r.reduce_only


def test_close_long():
    r = make('0', current='0.20')
    assert r.intent == OrderIntent.CLOSE
    assert r.order_size == D('0.20000')
    assert r.reduce_only


def test_open_short():
    r = make('-20')
    assert r.target_size == D('-0.20000')
    assert not r.is_buy


def test_reduce_short():
    r = make('-12', current='-0.20')
    assert r.intent == OrderIntent.REDUCE
    assert r.is_buy and r.reduce_only
    assert r.order_size == D('0.08000')


def test_reverse_long_to_short_splits_close_then_open():
    r = make('-10', current='0.10')
    assert r.intent == OrderIntent.REVERSE
    assert r.reduce_only and not r.is_buy
    assert r.order_size == D('0.10000')
    assert r.secondary is not None
    assert r.secondary.intent == OrderIntent.OPEN
    assert not r.secondary.reduce_only
    assert not r.secondary.is_buy


def test_partial_fill_residual_converges():
    r = make('12', current='0.15')
    assert r.order_size == D('0.03000')
    assert r.reduce_only


def test_below_minimum_preserves_target_and_sends_nothing():
    r = make('0.01')
    assert r.intent == OrderIntent.NONE
    assert r.order_size == 0
    assert r.target_size != 0
    assert 'stays on target' in (r.reason or '')


def test_unmanaged_margin_reduces_eligible_equity():
    master = MasterExposure('BTC', D('20'), D('60000'), D('1000000'))
    follower = FollowerState('u', D('10000'), D('5000'), D('0'), D('1'))
    r = plan(master, follower, SPEC)
    assert r.target_size == D('0.10000')


def test_cross_network_uses_master_mark_for_ratio_and_follower_mark_for_units():
    # Master: 1 BTC * $60k / $120k equity = 50% exposure.
    # Follower: $10k * 50% = $5k target notional, converted at the follower
    # testnet mark of $50k -> 0.1 BTC. Using the master mark here would be wrong.
    master = MasterExposure('BTC', D('1'), D('60000'), D('120000'))
    follower = FollowerState('u', D('10000'), D('0'), D('0'), D('1'))
    r = plan(master, follower, SPEC, follower_mark_price=D('50000'))
    assert r.target_size == D('0.1')
    assert r.order_size == D('0.10000')
    assert r.notional == D('5000.00000')


def test_round_price_preserves_large_integer_allowed_by_hyperliquid():
    assert round_price(D('123456'), sz_decimals=2) == D('123456')


def test_round_price_prefers_nearest_valid_integer_over_coarser_sig_fig_rounding():
    assert round_price(D('123456.7'), sz_decimals=2) == D('123457')


def test_round_price_still_applies_five_significant_figures_to_decimal_price():
    assert round_price(D('1234.56'), sz_decimals=2) == D('1234.6')


def test_round_price_still_applies_perp_decimal_place_limit():
    assert round_price(D('0.0012345'), sz_decimals=0) == D('0.001235')
