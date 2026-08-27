from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.action_errors import ActionErrorClass, ActionRetryPolicy, classify_action_error
from app.adapters.address_ratelimit import (
    ADDRESS_BACKOFF_SECONDS,
    AddressActionTracker,
    is_exchange_rate_limit_error,
)
from app.adapters.hyperliquid import HyperliquidAdapter, parse_order_response


class _FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.counters: dict[str, int] = {}

    async def ttl(self, key: str) -> int:
        return self.ttls.get(key, -2)

    async def set(self, key: str, value: str, *, ex: int | None = None):
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def hincrby(self, key: str, field: str, amount: int):
        bucket = self.hashes.setdefault(key, {})
        value = int(bucket.get(field, "0")) + amount
        bucket[field] = str(value)
        return value

    async def hset(self, key: str, mapping: dict[str, object]):
        bucket = self.hashes.setdefault(key, {})
        for field, value in mapping.items():
            bucket[field] = str(value)
        return len(mapping)

    async def hgetall(self, key: str):
        return dict(self.hashes.get(key, {}))

    async def incr(self, key: str):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]


class _FakeLimiter:
    def __init__(self, redis: _FakeRedis):
        self._redis = redis
        self.acquire = AsyncMock(return_value=None)


def test_rate_limit_error_classifier_is_specific_to_throttling():
    assert is_exchange_rate_limit_error(RuntimeError("429 Too Many Requests"))
    assert is_exchange_rate_limit_error(RuntimeError("address rate limited"))
    assert is_exchange_rate_limit_error(RuntimeError("RateLimitExceeded"))
    assert not is_exchange_rate_limit_error(RuntimeError("Insufficient margin"))
    assert not is_exchange_rate_limit_error(RuntimeError("nonce too low"))


def test_explicit_rate_limit_rejection_reconciles_but_http_429_stays_ambiguous():
    fixture = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"error": "User rate limited"}]},
        },
    }
    outcome = parse_order_response(fixture)
    assert outcome.state == "REJECTED"
    explicit = classify_action_error(outcome.reason)
    assert explicit.error_class is ActionErrorClass.TRANSIENT
    assert explicit.retry_policy is ActionRetryPolicy.RECONCILE

    transport = classify_action_error("429 Too Many Requests")
    assert transport.error_class is ActionErrorClass.UNCLASSIFIED
    assert transport.retry_policy is ActionRetryPolicy.NONE


@pytest.mark.asyncio
async def test_tracker_accounts_per_address_and_authoritative_snapshot():
    redis = _FakeRedis()
    tracker = AddressActionTracker(redis, "testnet")
    address = "0x" + "AA" * 20

    await tracker.record_action_attempt(address)
    await tracker.record_action_attempt(address.lower())
    await tracker.mark_throttled(address)
    await tracker.record_exchange_snapshot(
        address,
        {
            "cumVlm": "25000.5",
            "nRequestsUsed": 12000,
            "nRequestsCap": 35000,
            "nRequestsSurplus": 23000,
        },
    )

    snapshot = (await tracker.snapshot(address)).as_dict()
    assert snapshot["address"] == address.lower()
    assert snapshot["local_action_attempts"] == 2
    assert snapshot["local_throttle_count"] == 1
    assert snapshot["backoff_seconds_remaining"] == ADDRESS_BACKOFF_SECONDS
    assert snapshot["exchange"] == {
        "cum_volume": "25000.5",
        "requests_used": 12000,
        "requests_cap": 35000,
        "requests_surplus": 23000,
        "remaining_from_reported_fields": 23000,
        "snapshot_at_ms": snapshot["exchange"]["snapshot_at_ms"],
    }
    assert redis.counters["hypercopy:metrics:hl_address_action_attempt_count"] == 2
    assert redis.counters["hypercopy:metrics:hl_address_throttle_count"] == 1


@pytest.mark.asyncio
async def test_tracker_waits_only_for_the_throttled_address(monkeypatch):
    redis = _FakeRedis()
    tracker = AddressActionTracker(redis, "testnet")
    throttled = "0x" + "11" * 20
    other = "0x" + "22" * 20
    await tracker.mark_throttled(throttled)
    backoff_key = tracker._backoff_key(throttled)

    async def expire_after_sleep(_seconds: int):
        redis.ttls[backoff_key] = -2

    sleep = AsyncMock(side_effect=expire_after_sleep)
    monkeypatch.setattr("app.adapters.address_ratelimit.asyncio.sleep", sleep)

    waited = await tracker.wait_if_backed_off(throttled)
    not_waited = await tracker.wait_if_backed_off(other)

    assert waited == ADDRESS_BACKOFF_SECONDS
    assert not_waited == 0
    sleep.assert_awaited_once_with(ADDRESS_BACKOFF_SECONDS)
    assert redis.counters["hypercopy:metrics:hl_address_backoff_wait_count"] == 1


@pytest.mark.asyncio
async def test_signed_throttle_is_observed_without_blind_retry(monkeypatch):
    redis = _FakeRedis()
    limiter = _FakeLimiter(redis)
    monkeypatch.setattr("app.adapters.hyperliquid.Info", MagicMock())

    @asynccontextmanager
    async def unlocked(_signer_address: str):
        yield

    monkeypatch.setattr("app.adapters.hyperliquid.signer_action_lock", unlocked)
    adapter = HyperliquidAdapter(limiter, network="testnet")
    account = "0x" + "33" * 20
    signer = "0x" + "44" * 20
    submitted = AsyncMock(side_effect=RuntimeError("429 Too Many Requests"))
    official = {
        "cumVlm": "9000",
        "nRequestsUsed": 10001,
        "nRequestsCap": 19000,
        "nRequestsSurplus": 8999,
    }
    monkeypatch.setattr(adapter, "_call", submitted)
    monkeypatch.setattr(adapter, "user_rate_limit", AsyncMock(return_value=official))

    with pytest.raises(RuntimeError, match="429 Too Many Requests"):
        await adapter._signed_call(account, signer, lambda: {"status": "ok"})

    assert submitted.await_count == 1
    limiter.acquire.assert_awaited_once()
    snapshot = (await adapter.address_limits.snapshot(account)).as_dict()
    assert snapshot["local_action_attempts"] == 1
    assert snapshot["local_throttle_count"] == 1
    assert snapshot["backoff_seconds_remaining"] == ADDRESS_BACKOFF_SECONDS
    assert snapshot["exchange"]["requests_used"] == 10001
    assert snapshot["exchange"]["requests_cap"] == 19000
    assert snapshot["exchange"]["requests_surplus"] == 8999


@pytest.mark.asyncio
async def test_explicit_action_throttle_marks_backoff_without_transport_ambiguity(monkeypatch):
    redis = _FakeRedis()
    limiter = _FakeLimiter(redis)
    monkeypatch.setattr("app.adapters.hyperliquid.Info", MagicMock())
    adapter = HyperliquidAdapter(limiter, network="testnet")
    account = "0x" + "55" * 20
    official = {
        "cumVlm": "100",
        "nRequestsUsed": 10101,
        "nRequestsCap": 10100,
        "nRequestsSurplus": -1,
    }
    monkeypatch.setattr(adapter, "user_rate_limit", AsyncMock(return_value=official))

    await adapter._observe_explicit_address_throttle(account, "User rate limited")

    snapshot = (await adapter.address_limits.snapshot(account)).as_dict()
    assert snapshot["local_throttle_count"] == 1
    assert snapshot["backoff_seconds_remaining"] == ADDRESS_BACKOFF_SECONDS
    assert snapshot["exchange"]["requests_used"] == 10101
