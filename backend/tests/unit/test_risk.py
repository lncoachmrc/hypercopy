from decimal import Decimal

import pytest

from app.engine.risk import RiskAction, RiskContext, evaluate
from app.engine.sizing import AssetSpec, FollowerState, MasterExposure, OrderIntent, plan

D = Decimal
SPEC = AssetSpec('BTC', 5, 40)


def _plan(master='20', current='0'):
    return plan(MasterExposure('BTC', D(master), D('60000'), D('1000000')), FollowerState('u', D('10000'), D('0'), D(current), D('1')), SPEC)


@pytest.mark.parametrize('field', ['emergency_stop','global_pause','user_paused','drawdown_halt','daily_loss_halt','close_only'])
def test_business_risk_halts_block_opening(field):
    ctx = RiskContext(**{field: True})
    assert evaluate(_plan(), ctx).action == RiskAction.DENY


@pytest.mark.parametrize('field', ['emergency_stop','global_pause','user_paused','drawdown_halt','daily_loss_halt','close_only'])
def test_business_risk_halts_do_not_block_reduction(field):
    p = _plan(master='0', current='0.20')
    assert p.intent == OrderIntent.CLOSE
    ctx = RiskContext(**{field: True})
    assert evaluate(p, ctx).action == RiskAction.ALLOW


def test_subscription_expiry_does_not_block_close():
    p = _plan(master='0', current='0.20')
    assert evaluate(p, RiskContext(entitlement_active=False)).action == RiskAction.ALLOW


def test_credential_is_required_even_for_close():
    p = _plan(master='0', current='0.20')
    assert evaluate(p, RiskContext(credential_active=False)).action == RiskAction.DENY


def test_near_liquidation_blocks_new_exposure_but_not_close():
    assert evaluate(_plan(), RiskContext(near_liquidation=True)).action == RiskAction.DENY
    assert evaluate(_plan(master='0', current='0.20'), RiskContext(near_liquidation=True)).action == RiskAction.ALLOW


def test_max_notional_trims_instead_of_rejecting():
    d = evaluate(_plan(), RiskContext(max_notional_per_trade=D('5000'), max_total_exposure=D('20000'), max_asset_exposure=D('20000')))
    assert d.action == RiskAction.TRIM
    assert d.plan.notional == D('5000')


def test_leverage_headroom_trims_before_crossing_max_leverage():
    p = _plan()
    d = evaluate(p, RiskContext(
        account_equity=D('10000'), current_total_exposure=D('25000'),
        current_asset_exposure=D('0'), free_margin=D('10000'),
        current_leverage=D('2.5'), max_leverage=D('3'),
        max_notional_per_trade=D('20000'), max_total_exposure=D('100000'),
        max_asset_exposure=D('100000'),
    ))
    assert d.action == RiskAction.TRIM
    assert d.plan.notional == D('5000')


def test_free_margin_is_converted_to_notional_at_max_leverage():
    p = _plan()
    d = evaluate(p, RiskContext(
        account_equity=D('10000'), current_total_exposure=D('0'), free_margin=D('1000'),
        current_leverage=D('0'), max_leverage=D('3'),
        max_notional_per_trade=D('20000'), max_total_exposure=D('100000'),
        max_asset_exposure=D('100000'),
    ))
    assert d.action == RiskAction.TRIM
    assert d.plan.notional == D('3000')
