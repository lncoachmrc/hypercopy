from decimal import Decimal

from hypothesis import given, settings, strategies as st

from app.engine.risk import RiskAction, RiskContext, evaluate
from app.engine.sizing import (
    AssetSpec,
    FollowerState,
    MasterExposure,
    OrderIntent,
    compute_target,
    plan,
    round_price,
    round_size,
)

D = Decimal
PROPERTY_SETTINGS = settings(
    max_examples=100,
    derandomize=True,
    deadline=None,
    database=None,
)


def _decimal_ratio(value: int, scale: int = 1000) -> Decimal:
    return D(value) / D(scale)


@PROPERTY_SETTINGS
@given(
    raw=st.integers(min_value=0, max_value=10**12),
    sz_decimals=st.integers(min_value=0, max_value=6),
)
def test_round_size_never_overshoots_and_respects_lot_quantum(raw, sz_decimals):
    size = D(raw).scaleb(-8)
    rounded = round_size(size, sz_decimals)
    quantum = D(1).scaleb(-sz_decimals)

    assert D(0) <= rounded <= abs(size)
    assert rounded % quantum == 0


@PROPERTY_SETTINGS
@given(
    raw=st.integers(min_value=1, max_value=10**12),
    sz_decimals=st.integers(min_value=0, max_value=6),
)
def test_round_price_stays_positive_and_within_perp_decimal_limit(raw, sz_decimals):
    price = D(1) + D(raw).scaleb(-4)
    rounded = round_price(price, sz_decimals)
    max_places = max(6 - sz_decimals, 0)

    assert rounded > 0
    if rounded != rounded.to_integral_value():
        assert max(-rounded.as_tuple().exponent, 0) <= max_places


@PROPERTY_SETTINGS
@given(
    master_size=st.integers(min_value=-10_000, max_value=10_000).filter(lambda value: value != 0),
    master_price=st.integers(min_value=1, max_value=1_000_000),
    master_equity=st.integers(min_value=1, max_value=10_000_000),
    follower_equity=st.integers(min_value=1, max_value=10_000_000),
    unmanaged_bps=st.integers(min_value=0, max_value=10_000),
    multiplier_milli=st.integers(min_value=0, max_value=3_000),
)
def test_unmanaged_margin_never_increases_absolute_target(
    master_size,
    master_price,
    master_equity,
    follower_equity,
    unmanaged_bps,
    multiplier_milli,
):
    master = MasterExposure(
        'BTC',
        D(master_size),
        D(master_price),
        D(master_equity),
    )
    multiplier = _decimal_ratio(multiplier_milli)
    account_value = D(follower_equity)
    unmanaged_margin = account_value * D(unmanaged_bps) / D(10_000)
    without_unmanaged = FollowerState(
        'u',
        account_value,
        D(0),
        D(0),
        multiplier,
    )
    with_unmanaged = FollowerState(
        'u',
        account_value,
        unmanaged_margin,
        D(0),
        multiplier,
    )

    baseline = compute_target(master, without_unmanaged)
    reduced = compute_target(master, with_unmanaged)

    assert abs(reduced) <= abs(baseline)
    if reduced != 0:
        assert (reduced > 0) == (baseline > 0)


@PROPERTY_SETTINGS
@given(
    master_size=st.integers(min_value=-10_000, max_value=10_000),
    master_price=st.integers(min_value=1, max_value=1_000_000),
    master_equity=st.integers(min_value=1, max_value=10_000_000),
    follower_equity=st.integers(min_value=1, max_value=10_000_000),
    multiplier_milli=st.integers(min_value=0, max_value=3_000),
)
def test_non_positive_follower_equity_always_produces_zero_target(
    master_size,
    master_price,
    master_equity,
    follower_equity,
    multiplier_milli,
):
    master = MasterExposure(
        'BTC',
        D(master_size),
        D(master_price),
        D(master_equity),
    )
    multiplier = _decimal_ratio(multiplier_milli)
    follower = FollowerState(
        'u',
        -D(follower_equity),
        D(0),
        D(0),
        multiplier,
    )

    assert compute_target(master, follower) == 0


@PROPERTY_SETTINGS
@given(
    master_size=st.integers(min_value=-100_000, max_value=100_000),
    current_size=st.integers(min_value=-100_000, max_value=100_000),
    price=st.integers(min_value=1, max_value=1_000_000),
    sz_decimals=st.integers(min_value=0, max_value=6),
)
def test_reduce_only_plan_never_closes_more_than_current_position(
    master_size,
    current_size,
    price,
    sz_decimals,
):
    spec = AssetSpec('BTC', sz_decimals, 50)
    result = plan(
        MasterExposure('BTC', D(master_size), D(price), D('1000000')),
        FollowerState('u', D('10000'), D(0), D(current_size), D(1)),
        spec,
    )

    if result.reduce_only and result.intent is not OrderIntent.REVERSE:
        assert result.order_size <= abs(result.current_size)


@PROPERTY_SETTINGS
@given(
    master_size=st.integers(min_value=1, max_value=100_000),
    follower_equity=st.integers(min_value=1, max_value=1_000_000),
    price=st.integers(min_value=1, max_value=1_000_000),
    sz_decimals=st.integers(min_value=0, max_value=6),
)
def test_opening_order_never_executes_below_exchange_minimum(
    master_size,
    follower_equity,
    price,
    sz_decimals,
):
    result = plan(
        MasterExposure('BTC', D(master_size), D(price), D('1000000')),
        FollowerState('u', D(follower_equity), D(0), D(0), D(1)),
        AssetSpec('BTC', sz_decimals, 50),
    )

    if result.actionable:
        assert result.intent is OrderIntent.OPEN
        assert result.notional >= D('10')


BUSINESS_HALT_FIELDS = (
    'emergency_stop',
    'global_pause',
    'user_paused',
    'user_active',
    'entitlement_active',
    'close_only',
    'asset_allowed',
    'drawdown_halt',
    'daily_loss_halt',
    'near_liquidation',
)


@PROPERTY_SETTINGS
@given(blocked=st.sets(st.sampled_from(BUSINESS_HALT_FIELDS), min_size=1))
def test_any_business_halt_combination_denies_new_exposure(blocked):
    opening = plan(
        MasterExposure('BTC', D('20'), D('60000'), D('1000000')),
        FollowerState('u', D('10000')),
        AssetSpec('BTC', 5, 40),
    )
    overrides = {field: True for field in blocked}
    if 'user_active' in blocked:
        overrides['user_active'] = False
    if 'entitlement_active' in blocked:
        overrides['entitlement_active'] = False
    if 'asset_allowed' in blocked:
        overrides['asset_allowed'] = False

    decision = evaluate(opening, RiskContext(**overrides))

    assert opening.actionable
    assert decision.action is RiskAction.DENY


@PROPERTY_SETTINGS
@given(blocked=st.sets(st.sampled_from(BUSINESS_HALT_FIELDS), min_size=1))
def test_business_halts_do_not_prevent_safe_reduction(blocked):
    closing = plan(
        MasterExposure('BTC', D(0), D('60000'), D('1000000')),
        FollowerState('u', D('10000'), D(0), D('0.20'), D(1)),
        AssetSpec('BTC', 5, 40),
    )
    overrides = {field: True for field in blocked}
    if 'user_active' in blocked:
        overrides['user_active'] = False
    if 'entitlement_active' in blocked:
        overrides['entitlement_active'] = False
    if 'asset_allowed' in blocked:
        overrides['asset_allowed'] = False

    decision = evaluate(closing, RiskContext(**overrides))

    assert closing.intent is OrderIntent.CLOSE
    assert decision.action is RiskAction.ALLOW


@PROPERTY_SETTINGS
@given(
    master_size=st.integers(min_value=1, max_value=100_000),
    follower_equity=st.integers(min_value=1, max_value=1_000_000),
    price=st.integers(min_value=1, max_value=1_000_000),
    factor_bps=st.integers(min_value=7_000, max_value=10_000),
    max_trade=st.integers(min_value=1, max_value=100_000_000),
    max_total=st.integers(min_value=1, max_value=100_000_000),
    max_asset=st.integers(min_value=1, max_value=100_000_000),
    current_total=st.integers(min_value=0, max_value=100_000_000),
    current_asset=st.integers(min_value=0, max_value=100_000_000),
    free_margin=st.integers(min_value=0, max_value=10_000_000),
    max_leverage=st.integers(min_value=1, max_value=50),
)
def test_ai_scaled_opening_cannot_bypass_deterministic_risk_caps(
    master_size,
    follower_equity,
    price,
    factor_bps,
    max_trade,
    max_total,
    max_asset,
    current_total,
    current_asset,
    free_margin,
    max_leverage,
):
    factor = D(factor_bps) / D(10_000)
    deterministic_master = MasterExposure(
        'BTC',
        D(master_size),
        D(price),
        D('1000000'),
    )
    follower = FollowerState('u', D(follower_equity))
    deterministic_target = compute_target(deterministic_master, follower)
    ai_master = MasterExposure(
        'BTC',
        D(master_size) * factor,
        D(price),
        D('1000000'),
    )
    ai_target = compute_target(ai_master, follower)
    sizing = plan(ai_master, follower, AssetSpec('BTC', 5, 50))
    ctx = RiskContext(
        current_total_exposure=D(current_total),
        current_asset_exposure=D(current_asset),
        free_margin=D(free_margin),
        account_equity=D(follower_equity),
        max_notional_per_trade=D(max_trade),
        max_total_exposure=D(max_total),
        max_asset_exposure=D(max_asset),
        max_leverage=D(max_leverage),
    )
    decision = evaluate(sizing, ctx)
    allowed_notional = min(
        ctx.max_notional_per_trade,
        max(ctx.max_total_exposure - ctx.current_total_exposure, D(0)),
        max(ctx.max_asset_exposure - ctx.current_asset_exposure, D(0)),
        max(ctx.free_margin, D(0)) * max(ctx.max_leverage, D(1)),
        max(
            ctx.max_leverage * max(ctx.account_equity, D(0))
            - ctx.current_total_exposure,
            D(0),
        ),
    )

    assert abs(ai_target) <= abs(deterministic_target)
    if sizing.actionable and allowed_notional < D('10'):
        assert decision.action is RiskAction.DENY
    if decision.action in {RiskAction.ALLOW, RiskAction.TRIM}:
        assert decision.plan.notional >= D('10')
        assert decision.plan.notional <= allowed_notional
    if decision.action is RiskAction.TRIM:
        assert decision.plan.order_size == round_size(decision.plan.order_size, 5)
