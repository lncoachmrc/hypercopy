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
        self.pttls: dict[str, int] = {}
        self.counters: dict[str, int] = {}
        self.fail_set = False

    async def ttl(self, key: str) -> int:
        remaining = await self.pttl(key)
        if remaining < 0:
            return remaining
        return remaining // 1000

    async def pttl(self, key: str) -> int:
        if key not in self.values:
            return -2
        return self.pttls.get(key, -1)

    async def exists(self, key: str) -> int:
        return int(key in self.values)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ):
        if self.fail_set:
            raise RuntimeError("redis unavailable")
        if nx and key in self.values:
            return None
        self.values[key] = value
        if px is not None:
            self.pttls[key] = px
        elif ex is not None:
            self.pttls[key] = ex * 1000
        else:
            self.pttls.pop(key, None)
        return True

    async def delete(self, *keys: str):
        deleted = 0
        for key in keys:
            if key in self.values:
                deleted += 1
            self.values.pop(key, None)
            self.pttls.pop(key, None)
        return deleted

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

    def advance(self, seconds: float) -> None:
        elapsed = int(seconds * 1000)
        for key, remaining in list(self.pttls.items()):
            updated = remaining - elapsed
            if updated <= 0:
                self.values.pop(key, None)
                self.pttls.pop(key, None)
            else:
                self.pttls[key] = updated


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


@pytest.mark.parametrize(
    "reason",
    [
        "User rate limited",
        "RateLimitExceeded",
        "UserRateLimitExceeded",
        "AddressRateLimitExceeded",
    ],
)
def test_explicit_rate_limit_rejections_delegate_to_fresh_reconciliation(reason: str):
    fixture = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"error": reason}]},
        },
    }
    outcome = parse_order_response(fixture)
    assert outcome.state == "REJECTED"
    explicit = classify_action_error(outcome.reason)
    assert explicit.error_class is ActionErrorClass.TRANSIENT
    assert explicit.retry_policy is ActionRetryPolicy.RECONCILE


def test_http_429_stays_out_of_explicit_action_taxonomy():
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
    assert snapshot["throttled_mode"] is False
    assert snapshot["local_action_attempts"] == 2
    assert snapshot["local_throttle_count"] == 1
    assert snapshot["backoff_seconds_remaining"] == 0
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
async def test_quota_exhausted_mode_atomically_spaces_each_subsequent_action(monkeypatch):
    redis = _FakeRedis()
    tracker = AddressActionTracker(redis, "testnet")
    throttled = "0x" + "11" * 20
    other = "0x" + "22" * 20

    await tracker.mark_throttled(throttled)
    await tracker.record_exchange_snapshot(
        throttled,
        {
            "cumVlm": "100",
            "nRequestsUsed": 10100,
            "nRequestsCap": 10100,
            "nRequestsSurplus": 0,
        },
    )

    async def advance_clock(seconds: float):
        redis.advance(seconds)

    sleep = AsyncMock(side_effect=advance_clock)
    monkeypatch.setattr("app.adapters.address_ratelimit.asyncio.sleep", sleep)

    first_wait = await tracker.wait_if_backed_off(throttled)
    # The first waiter reserves a fresh 10-second slot before it is released.
    assert await redis.pttl(tracker._slot_key(throttled)) == ADDRESS_BACKOFF_SECONDS * 1000
    second_wait = await tracker.wait_if_backed_off(throttled)
    not_waited = await tracker.wait_if_backed_off(other)

    assert first_wait == float(ADDRESS_BACKOFF_SECONDS)
    assert second_wait == float(ADDRESS_BACKOFF_SECONDS)
    assert not_waited == 0
    assert sleep.await_count == 2
    assert [call.args[0] for call in sleep.await_args_list] == [
        float(ADDRESS_BACKOFF_SECONDS),
        float(ADDRESS_BACKOFF_SECONDS),
    ]
    assert redis.counters["hypercopy:metrics:hl_address_backoff_wait_count"] == 2


@pytest.mark.asyncio
async def test_signed_transport_throttle_is_observed_without_blind_retry_or_diagnostic(monkeypatch):
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
    submitted = MagicMock(side_effect=RuntimeError("429 Too Many Requests"))
    diagnostic = AsyncMock()
    monkeypatch.setattr(adapter, "user_rate_limit", diagnostic)

    with pytest.raises(RuntimeError, match="429 Too Many Requests"):
        await adapter._signed_call(account, signer, submitted)

    submitted.assert_called_once()
    diagnostic.assert_not_awaited()
    limiter.acquire.assert_awaited_once()
    snapshot = (await adapter.address_limits.snapshot(account)).as_dict()
    assert snapshot["throttled_mode"] is False
    assert snapshot["local_action_attempts"] == 1
    assert snapshot["local_throttle_count"] == 1
    assert snapshot["backoff_seconds_remaining"] == ADDRESS_BACKOFF_SECONDS
    assert snapshot["exchange"]["requests_used"] is None


@pytest.mark.asyncio
async def test_explicit_action_throttle_diagnostic_sets_sustained_mode_and_snapshot(monkeypatch):
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

    # Direct diagnostic refresh does not install the one-shot throttle; the real
    # signed path does that inside _signed_call while the signer lock is held.
    await adapter._observe_explicit_address_throttle(account, "User rate limited")

    snapshot = (await adapter.address_limits.snapshot(account)).as_dict()
    assert snapshot["throttled_mode"] is True
    assert snapshot["local_throttle_count"] == 0
    assert snapshot["backoff_seconds_remaining"] == 0
    assert snapshot["exchange"]["requests_used"] == 10101


@pytest.mark.asyncio
async def test_explicit_rejection_survives_redis_throttle_tracking_failure(monkeypatch):
    redis = _FakeRedis()
    limiter = _FakeLimiter(redis)
    monkeypatch.setattr("app.adapters.hyperliquid.Info", MagicMock())
    adapter = HyperliquidAdapter(limiter, network="testnet")
    account = "0x" + "66" * 20
    redis.fail_set = True
    monkeypatch.setattr(
        adapter,
        "user_rate_limit",
        AsyncMock(
            return_value={
                "cumVlm": "100",
                "nRequestsUsed": 10101,
                "nRequestsCap": 10100,
                "nRequestsSurplus": -1,
            }
        ),
    )
    fixture = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"error": "User rate limited"}]},
        },
    }
    outcome = parse_order_response(fixture)
    assert outcome.state == "REJECTED"

    # Observability failure must not replace the exchange's known no-effect result.
    await adapter._observe_explicit_address_throttle(account, outcome.reason)
    assert outcome.state == "REJECTED"
