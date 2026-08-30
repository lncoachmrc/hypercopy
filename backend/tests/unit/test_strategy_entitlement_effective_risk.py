from decimal import Decimal
from types import SimpleNamespace
import uuid

import pytest

from app.api import user as user_api
from app.schemas.user import RiskProfileIn
from app.services.effective_risk import resolve_effective_risk

D = Decimal


def _selected_risk(**overrides):
    values = {
        'multiplier': D('1'),
        'max_notional_per_trade': D('1100'),
        'max_total_exposure': D('4500'),
        'max_asset_exposure': D('1100'),
        'max_leverage': D('40'),
        'max_positions': 50,
        'max_drawdown_pct': D('20'),
        'max_daily_loss_pct': D('10'),
        'min_notional': D('10'),
        'max_slippage_bps': 50,
        'close_only': False,
        'allow_assets': [],
        'block_assets': [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_trial_caps_complete_strategy_without_mutating_selection():
    selected = _selected_risk()
    effective = resolve_effective_risk(selected, {
        'limits': {
            'max_multiplier': 1,
            'max_notional_per_trade': 500,
            'max_positions': 3,
            'max_equity_usd': 1000,
        }
    })

    assert selected.max_positions == 50
    assert selected.max_notional_per_trade == D('1100')
    assert effective.multiplier == D('1')
    assert effective.max_positions == 3
    assert effective.max_notional_per_trade == D('500')


def test_plan_upgrade_releases_runtime_cap_without_resaving_strategy():
    selected = _selected_risk()

    trial = resolve_effective_risk(selected, {
        'limits': {'max_multiplier': 1, 'max_notional_per_trade': 500, 'max_positions': 3}
    })
    starter = resolve_effective_risk(selected, {
        'limits': {'max_multiplier': 10, 'max_notional_per_trade': 2500, 'max_positions': 100}
    })

    assert trial.max_positions == 3
    assert trial.max_notional_per_trade == D('500')
    assert starter.max_positions == 50
    assert starter.max_notional_per_trade == D('1100')
    assert selected.max_positions == 50


def test_plan_downgrade_is_applied_immediately_at_runtime():
    selected = _selected_risk(multiplier=D('2.5'), max_notional_per_trade=D('7000'), max_positions=80)

    pro = resolve_effective_risk(selected, {
        'limits': {'max_multiplier': 10, 'max_notional_per_trade': 10000, 'max_positions': 100}
    })
    trial = resolve_effective_risk(selected, {
        'limits': {'max_multiplier': 1, 'max_notional_per_trade': 500, 'max_positions': 3}
    })

    assert pro.multiplier == D('2.5')
    assert pro.max_notional_per_trade == D('7000')
    assert pro.max_positions == 80
    assert trial.multiplier == D('1')
    assert trial.max_notional_per_trade == D('500')
    assert trial.max_positions == 3


def test_exchange_leverage_remains_an_independent_runtime_ceiling():
    selected = _selected_risk(max_leverage=D('40'))

    effective = resolve_effective_risk(
        selected,
        {'limits': {'max_positions': 100}},
        exchange_max_leverage=20,
    )

    assert selected.max_leverage == D('40')
    assert effective.max_leverage == D('20')


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _Db:
    def __init__(self, row):
        self.row = row
        self.commits = 0

    async def execute(self, _query):
        return _Result(self.row)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_put_risk_persists_selected_values_without_entitlement_clipping(monkeypatch):
    row = _selected_risk(max_positions=3, max_notional_per_trade=D('500'))
    db = _Db(row)
    actor = SimpleNamespace(id=uuid.uuid4())
    body = RiskProfileIn(
        multiplier=D('1'),
        max_notional_per_trade=D('1100'),
        max_total_exposure=D('4500'),
        max_asset_exposure=D('1100'),
        max_leverage=D('40'),
        max_positions=50,
        max_drawdown_pct=D('20'),
        max_daily_loss_pct=D('10'),
        min_notional=D('10'),
        max_slippage_bps=50,
        close_only=False,
        allow_assets=[],
        block_assets=[],
    )

    async def forbidden_entitlement(*_args, **_kwargs):
        raise AssertionError('put_risk must not apply commercial entitlement caps')

    async def fake_audit(*_args, **_kwargs):
        return None

    async def fake_get_risk(_user, _db):
        return {
            'multiplier': row.multiplier,
            'max_notional_per_trade': row.max_notional_per_trade,
            'max_positions': row.max_positions,
        }

    monkeypatch.setattr(user_api, 'entitlement', forbidden_entitlement)
    monkeypatch.setattr(user_api, 'audit', fake_audit)
    monkeypatch.setattr(user_api, 'get_risk', fake_get_risk)

    result = await user_api.put_risk(body, user=actor, db=db)

    assert db.commits == 1
    assert row.multiplier == D('1')
    assert row.max_notional_per_trade == D('1100')
    assert row.max_positions == 50
    assert result['max_positions'] == 50
