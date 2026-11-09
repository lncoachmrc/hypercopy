from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.ai_exit_feedback import read_exit_feedback


@pytest.mark.asyncio
async def test_cold_start_hides_actionable_statistics_from_model_input():
    now = datetime.now(UTC)
    record = dict(source_cycle_id='old', decision_id='d1', before_pnl='5', after_pnl='2',
        position='-1', entry='100', outcome_entry='100', decided_at=now-timedelta(hours=2),
        observed_at=now-timedelta(minutes=90))

    class DB:
        async def execute(self, stmt, params):
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: [record]))

    result = await read_exit_feedback(DB(), user_id='u', epoch_id='e', provider='risex',
        network='testnet', asset='BTC', side='SHORT', basis='risex_copy_shadow_estimate',
        pnl_complete=False, funding_model='not_modeled_fee_10bps', current_cycle='current', as_of=now)
    assert result['status'] == 'warming_up'
    assert result['sample_count'] == 1
    assert result['mean_close_advantage_bps'] is None
    assert result['close_advantage_posterior_mean'] is None
    assert result['close_better_count'] is None
    assert result['hold_better_count'] is None
