from datetime import UTC, datetime, timedelta

import pytest

from app.services.public_performance import _pct, _range_start


def test_pct_normalizes_realized_net_pnl_against_baseline_equity():
    assert _pct(30.0, 3_000.0) == pytest.approx(1.0)
    assert _pct(-15.0, 3_000.0) == pytest.approx(-0.5)


def test_pct_returns_zero_without_valid_baseline():
    assert _pct(10.0, None) == 0.0
    assert _pct(10.0, 0.0) == 0.0


def test_all_range_never_reaches_before_operational_start():
    started_at = datetime(2026, 8, 17, 23, 34, 57, tzinfo=UTC)
    now = datetime(2026, 8, 23, 14, 0, tzinfo=UTC)
    start, bucket_seconds = _range_start(now, started_at, 'all')
    assert start == started_at
    assert bucket_seconds == 24 * 60 * 60


def test_short_range_is_clamped_to_operational_start():
    now = datetime(2026, 8, 18, 0, 0, tzinfo=UTC)
    started_at = now - timedelta(hours=2)
    start, _ = _range_start(now, started_at, '1d')
    assert start == started_at
