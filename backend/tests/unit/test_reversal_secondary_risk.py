from decimal import Decimal

from app.engine.risk import RiskAction, RiskContext, evaluate
from app.engine.sizing import AssetSpec, FollowerState, MasterExposure, OrderIntent, plan

D = Decimal
SPEC = AssetSpec('BTC', 5, 40)


def _reversal():
    result = plan(
        MasterExposure('BTC', D('-2'), D('60000'), D('1000000')),
        FollowerState('u', D('10000'), D('0'), D('0.02'), D('1')),
        SPEC,
    )
    assert result.intent == OrderIntent.REVERSE
    assert result.reduce_only is True
    assert result.secondary is not None
    return result


def _ctx(**overrides):
    values = dict(
        current_total_exposure=D('1200'),
        current_asset_exposure=D('1200'),
        free_margin=D('10000'),
        account_equity=D('10000'),
        current_leverage=D('0.12'),
        open_positions=1,
        is_new_market=False,
        max_notional_per_trade=D('5000'),
        max_total_exposure=D('100000'),
        max_asset_exposure=D('100000'),
        max_leverage=D('3'),
        max_positions=50,
    )
    values.update(overrides)
    return RiskContext(**values)


def test_reversal_over_position_cap_closes_but_suppresses_reopen():
    decision = evaluate(
        _reversal(),
        _ctx(
            current_total_exposure=D('4200'),
            current_asset_exposure=D('1200'),
            open_positions=5,
            max_positions=3,
        ),
    )

    assert decision.action == RiskAction.ALLOW
    assert decision.plan.reduce_only is True
    assert decision.plan.secondary is None
    assert 'reductions only' in (decision.reason or '')


def test_reversal_at_position_cap_can_replace_same_market_after_flattening():
    decision = evaluate(
        _reversal(),
        _ctx(open_positions=3, max_positions=3),
    )

    assert decision.action == RiskAction.ALLOW
    assert decision.plan.secondary is not None
    assert decision.plan.secondary.intent == OrderIntent.OPEN


def test_reversal_secondary_is_trimmed_by_per_trade_cap():
    decision = evaluate(
        _reversal(),
        _ctx(max_notional_per_trade=D('500')),
    )

    assert decision.action == RiskAction.ALLOW
    assert decision.plan.order_size == D('0.02000')
    assert decision.plan.secondary is not None
    assert decision.plan.secondary.order_size == D('0.00833')
    assert decision.plan.secondary.notional == D('499.80000')
    assert 'Trimmed to risk cap' in (decision.reason or '')


def test_reversal_during_global_pause_flattens_without_reopening():
    decision = evaluate(_reversal(), _ctx(global_pause=True))

    assert decision.action == RiskAction.ALLOW
    assert decision.plan.secondary is None
    assert 'Global pause is active' in (decision.reason or '')
