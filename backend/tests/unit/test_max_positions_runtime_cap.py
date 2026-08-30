from decimal import Decimal

from app.engine.risk import RiskAction, RiskContext, evaluate
from app.engine.sizing import AssetSpec, FollowerState, MasterExposure, plan

D = Decimal
SPEC = AssetSpec('BTC', 5, 40)


def _plan(master: str, current: str):
    return plan(
        MasterExposure('BTC', D(master), D('60000'), D('1000000')),
        FollowerState('u', D('10000'), D('0'), D(current), D('1')),
        SPEC,
    )


def test_over_cap_existing_market_cannot_increase_exposure():
    decision = evaluate(
        _plan(master='20', current='0.10'),
        RiskContext(open_positions=4, max_positions=3, is_new_market=False),
    )

    assert decision.action == RiskAction.DENY
    assert 'reductions only' in (decision.reason or '')


def test_over_cap_book_can_still_reduce_existing_position():
    decision = evaluate(
        _plan(master='5', current='0.10'),
        RiskContext(open_positions=4, max_positions=3, is_new_market=False),
    )

    assert decision.action == RiskAction.ALLOW


def test_exactly_at_cap_existing_market_can_still_rebalance_up():
    decision = evaluate(
        _plan(master='20', current='0.10'),
        RiskContext(
            open_positions=3,
            max_positions=3,
            is_new_market=False,
            max_notional_per_trade=D('100000'),
            max_total_exposure=D('100000'),
            max_asset_exposure=D('100000'),
            max_leverage=D('40'),
        ),
    )

    assert decision.action in {RiskAction.ALLOW, RiskAction.TRIM}
