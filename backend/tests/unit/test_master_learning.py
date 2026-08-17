from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.services.master_learning import learn_master_strategy


def _event(asset, at, start, after, size, price='1', equity='1000'):
    return SimpleNamespace(
        asset=asset,
        event_ts=at,
        start_position=Decimal(start),
        position_after=Decimal(after),
        size=Decimal(size),
        price=Decimal(price),
        master_equity=Decimal(equity),
    )


def test_learning_detects_open_scale_in_close_and_persistence():
    t0 = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    events = [
        _event('BTC', t0, '0', '10', '10'),
        _event('BTC', t0 + timedelta(minutes=10), '10', '20', '10'),
        _event('BTC', t0 + timedelta(minutes=70), '20', '0', '20'),
        _event('MICRO', t0 + timedelta(minutes=80), '0', '1', '1'),
    ]

    learned = learn_master_strategy(events)
    btc = learned['assets']['BTC']

    assert learned['event_count'] == 4
    assert learned['asset_count'] == 2
    assert btc['openings'] == 1
    assert btc['scale_ins'] == 1
    assert btc['closings'] == 1
    assert btc['avg_holding_minutes'] == 70
    assert 0 <= btc['persistence_score'] <= 1
    assert learned['micro_fill_ratio'] == 0.25


def test_learning_empty_history_is_safe():
    learned = learn_master_strategy([])
    assert learned['event_count'] == 0
    assert learned['assets'] == {}
