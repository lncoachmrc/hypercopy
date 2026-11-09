from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import importlib

import pytest


def module():
    try:
        m = importlib.import_module('app.services.ai_exit_feedback')
    except ModuleNotFoundError:
        m = None
    assert callable(getattr(m, 'summarize_feedback', None)), 'missing causal feedback implementation'
    return m


NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


def row(cycle='old-1', before='5', after='2'):
    return dict(source_cycle_id=cycle, decision_id=cycle, before_pnl=before,
                after_pnl=after, position='-1', entry='100', outcome_entry='100',
                decided_at=NOW-timedelta(hours=2), observed_at=NOW-timedelta(minutes=90))


def test_empty_feedback_is_not_a_trading_signal():
    out = module().summarize_feedback([], as_of=NOW, current_cycle='now')
    assert out['status'] == 'warming_up'
    assert out['sample_count'] == 0
    assert out['promotion_allowed'] is False


def test_feedback_learns_from_both_good_and_bad_early_exits():
    rows = [row(f'c{i}', after='2' if i < 10 else '8') for i in range(20)]
    out = module().summarize_feedback(rows, as_of=NOW, current_cycle='now')
    assert out['status'] == 'observed'
    assert out['sample_count'] == 20
    assert D(out['mean_close_advantage_bps']) == 0
    assert D(out['close_advantage_posterior_mean']) == D('0.5')
    assert out['outcome_kind'] == 'sampled_30m_counterfactual'


def test_repeated_observations_of_one_source_cycle_do_not_inflate_evidence():
    out = module().summarize_feedback([row() for _ in range(100)], as_of=NOW, current_cycle='now')
    assert out['sample_count'] == 1
    assert out['status'] == 'warming_up'


@pytest.mark.parametrize('change', [
    {'source_cycle_id':'now'}, {'observed_at': NOW+timedelta(seconds=1)},
    {'observed_at': NOW-timedelta(hours=2)+timedelta(minutes=29)},
    {'observed_at': NOW-timedelta(hours=2)+timedelta(minutes=36)},
    {'before_pnl':'NaN'}, {'after_pnl':'Infinity'}, {'entry':'0'},
    {'position':'0'}, {'outcome_entry':'99'}, {'before_pnl':'-1'},
])
def test_ineligible_or_future_feedback_is_excluded(change):
    out = module().summarize_feedback([{**row(), **change}], as_of=NOW, current_cycle='now')
    assert out['sample_count'] == 0


def test_feedback_fingerprint_is_order_independent_and_changes_with_outcome():
    m = module()
    a, b = row('one'), row('two')
    one = m.summarize_feedback([a,b], as_of=NOW, current_cycle='now')
    two = m.summarize_feedback([b,a], as_of=NOW, current_cycle='now')
    changed = m.summarize_feedback([a,{**b,'after_pnl':'3'}], as_of=NOW, current_cycle='now')
    assert one['evidence_hash'] == two['evidence_hash']
    assert one['evidence_hash'] != changed['evidence_hash']
