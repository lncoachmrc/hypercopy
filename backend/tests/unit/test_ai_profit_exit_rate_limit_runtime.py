from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from app.adapters.hyperliquid import (
    HyperliquidAdapter,
    WEIGHT_USER_FUNDING_MAX,
    _user_funding_response_weight,
    _variable_info_response_weight,
)
from app.adapters.ratelimit import (
    Budget,
    Priority,
    RateLimitReservation,
    WEIGHT_USER_FILLS_MAX,
    WeightedRateLimiter,
)
from app.services.ai_profit_exit_decision import evaluate_profit_exit_portfolio
from app.workers import ai_intelligence_worker


def test_variable_info_weight_matches_hyperliquid_item_accounting() -> None:
    assert _variable_info_response_weight([]) == 20
    assert _variable_info_response_weight([{}]) == 21
    assert _variable_info_response_weight([{}] * 20) == 21
    assert _variable_info_response_weight([{}] * 21) == 22
    assert _variable_info_response_weight([{}] * 2000) == WEIGHT_USER_FILLS_MAX
    assert _variable_info_response_weight({"malformed": True}) == WEIGHT_USER_FILLS_MAX


def test_reconcile_lane_fits_one_bounded_cold_profit_exit_cycle() -> None:
    budget = Budget()

    # RECONCILE-lane worst case for one usable observation:
    # clearinghouseState(2) + userAbstraction(20) + allMids(2)
    # + max non-truncated fills(120) + max non-truncated funding(45)
    # + userFees(20) = 209.
    assert WEIGHT_USER_FUNDING_MAX == 45
    assert budget.reconcile >= 209
    assert budget.orders == 560
    assert budget.master_state == 300
    assert (
        budget.orders
        + budget.reconcile
        + budget.diagnostic
        + budget.master_state
        + budget.metadata
        + budget.reserve
        == budget.total_per_minute
    )


def test_funding_weight_is_bounded_to_500_row_endpoint_limit() -> None:
    assert _user_funding_response_weight([]) == 20
    assert _user_funding_response_weight([{}] * 499) == 45
    assert _user_funding_response_weight([{}] * 500) == 45


class _DynamicLimiter:
    def __init__(self, *, fail_settlement: bool = False) -> None:
        self._redis = object()
        self.fail_settlement = fail_settlement
        self.reserved: list[tuple[int, Priority, float]] = []
        self.settled: list[tuple[RateLimitReservation, int]] = []

    async def reserve(
        self,
        weight: int,
        priority: Priority,
        *,
        timeout: float,
        poll: float = 0.25,
    ) -> RateLimitReservation:
        self.reserved.append((weight, priority, float(timeout)))
        return RateLimitReservation(
            member=f"{weight}:test",
            priority=priority,
            reserved_weight=weight,
        )

    async def settle(
        self,
        reservation: RateLimitReservation,
        actual_weight: int,
    ) -> None:
        self.settled.append((reservation, actual_weight))
        if self.fail_settlement:
            raise RuntimeError("redis unavailable")


@pytest.mark.asyncio
async def test_dynamic_read_reserves_worst_case_then_settles_actual_weight(monkeypatch) -> None:
    limiter = _DynamicLimiter()
    adapter = HyperliquidAdapter(limiter, network="testnet")
    call = AsyncMock(return_value=[{"row": 1}, {"row": 2}])
    monkeypatch.setattr(adapter, "_call", call)

    response = await adapter._read(
        lambda: None,
        weight=WEIGHT_USER_FILLS_MAX,
        priority=Priority.RECONCILE,
        timeout=30,
        response_weight=_variable_info_response_weight,
    )

    assert response == [{"row": 1}, {"row": 2}]
    assert limiter.reserved == [
        (WEIGHT_USER_FILLS_MAX, Priority.RECONCILE, 30.0)
    ]
    assert len(limiter.settled) == 1
    assert limiter.settled[0][1] == 21
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_settlement_failure_never_replays_completed_hyperliquid_read(monkeypatch) -> None:
    limiter = _DynamicLimiter(fail_settlement=True)
    adapter = HyperliquidAdapter(limiter, network="testnet")
    call = AsyncMock(return_value=[])
    monkeypatch.setattr(adapter, "_call", call)

    response = await adapter._read(
        lambda: None,
        weight=WEIGHT_USER_FILLS_MAX,
        priority=Priority.RECONCILE,
        timeout=30,
        response_weight=_variable_info_response_weight,
    )

    assert response == []
    assert call.await_count == 1
    assert limiter.settled[0][1] == 20


class _EvalRedis:
    def __init__(self) -> None:
        self.calls = []

    async def eval(self, *args):
        self.calls.append(args)
        return 1


@pytest.mark.asyncio
async def test_rate_limit_settlement_is_downward_only_and_atomic() -> None:
    redis = _EvalRedis()
    limiter = WeightedRateLimiter(redis, Budget())
    reservation = RateLimitReservation(
        member="120:abc",
        priority=Priority.RECONCILE,
        reserved_weight=120,
    )

    await limiter.settle(reservation, 21)

    assert len(redis.calls) == 1
    call = redis.calls[0]
    assert call[1] == 2
    assert call[2].endswith("hl:bucket:rest")
    assert call[3].endswith("hl:bucket:rest:RECONCILE")
    assert call[-3:] == ("120:abc", "21:abc", 21)

    with pytest.raises(ValueError):
        await limiter.settle(reservation, 121)


def test_profit_exit_evaluator_forbids_unlimited_operational_readers() -> None:
    source = inspect.getsource(evaluate_profit_exit_portfolio)
    assert "HyperliquidAdapter(None" not in source
    assert 'master_hl.limiter is None' in source
    assert 'follower_hl.limiter is None' in source
    assert "priority=Priority.MASTER_STATE" in source


def test_ai_worker_owns_one_shared_limiter_and_caches_followers(monkeypatch) -> None:
    redis = object()
    limiter = object()
    adapters: list[tuple[object, str]] = []

    monkeypatch.setattr(ai_intelligence_worker, "redis_client", lambda: redis)
    monkeypatch.setattr(
        ai_intelligence_worker,
        "WeightedRateLimiter",
        lambda redis_arg, _budget: limiter if redis_arg is redis else None,
    )

    class _Adapter:
        def __init__(self, limiter_arg, *, network):
            adapters.append((limiter_arg, network))
            self.limiter = limiter_arg
            self.network = network

    monkeypatch.setattr(ai_intelligence_worker, "HyperliquidAdapter", _Adapter)

    worker = ai_intelligence_worker.AIIntelligenceWorker()

    assert worker.limiter is limiter
    assert adapters[0][0] is limiter

    first = worker._follower_hl("mainnet")
    second = worker._follower_hl("mainnet")
    other = worker._follower_hl("testnet")

    assert first is second
    assert other is not first
    assert all(limiter_arg is limiter for limiter_arg, _network in adapters)


def test_ai_worker_passes_shared_readers_into_profit_exit_evaluator() -> None:
    source = inspect.getsource(
        ai_intelligence_worker.AIIntelligenceWorker._run_profit_exit_evaluation
    )
    assert "master_hl=self.master_hl" in source
    assert "follower_hl_for_network=self._follower_hl" in source
