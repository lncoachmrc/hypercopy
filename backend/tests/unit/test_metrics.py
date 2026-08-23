from datetime import UTC, datetime, timedelta

import pytest

from app.services.metrics import (
    SHARPE_MIN_OBSERVATIONS,
    _annualized_sharpe,
    _completed_daily_realized_returns,
)


def test_sharpe_requires_minimum_completed_days():
    value, status = _annualized_sharpe([0.01] * (SHARPE_MIN_OBSERVATIONS - 1))
    assert value is None
    assert status == 'collecting'


def test_sharpe_reports_zero_variance_separately():
    value, status = _annualized_sharpe([0.0] * SHARPE_MIN_OBSERVATIONS)
    assert value is None
    assert status == 'zero_variance'


def test_sharpe_is_annualized_from_daily_returns():
    returns = [0.01, -0.004] * 10
    value, status = _annualized_sharpe(returns)
    assert status == 'ready'
    assert value is not None
    assert value == pytest.approx(8.0327, rel=1e-3)


def test_daily_returns_use_realized_pnl_and_day_opening_equity_only():
    started_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    now = datetime(2026, 1, 25, 12, 0, tzinfo=UTC)
    points: list[tuple[datetime, float]] = []
    realized = {}

    # The partial first day is intentionally excluded. Each following completed
    # day opens at 1,000 USDC and realizes +10 USDC net.
    for offset in range(1, 24):
        day = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=offset)
        opening = 2_000.0 if offset >= 12 else 1_000.0
        points.append((day + timedelta(minutes=1), opening))
        realized[day.date()] = 10.0

    # A same-day capital jump must not be interpreted as strategy return.
    points.append((datetime(2026, 1, 10, 12, 0, tzinfo=UTC), 5_000.0))
    points.sort(key=lambda item: item[0])

    returns = _completed_daily_realized_returns(
        points,
        realized,
        started_at=started_at,
        now=now,
    )

    assert len(returns) == 23
    assert returns[8] == pytest.approx(0.01)  # Jan 10: midday jump ignored.
    assert returns[11] == pytest.approx(0.005)  # Jan 13: higher opening capital.


def test_daily_returns_exclude_current_incomplete_utc_day():
    started_at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 22, 12, 0, tzinfo=UTC)
    points = []
    realized = {}
    for offset in range(21):
        day = started_at + timedelta(days=offset)
        points.append((day + timedelta(minutes=1), 1_000.0))
        realized[day.date()] = 1.0

    points.append((datetime(2026, 1, 22, 0, 1, tzinfo=UTC), 1_000.0))
    realized[datetime(2026, 1, 22, tzinfo=UTC).date()] = 500.0

    returns = _completed_daily_realized_returns(
        points,
        realized,
        started_at=started_at,
        now=now,
    )

    assert len(returns) == 21
    assert all(value == pytest.approx(0.001) for value in returns)
