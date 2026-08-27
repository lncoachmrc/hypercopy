from __future__ import annotations

import pytest

from app.adapters.address_ratelimit import (
    ADDRESS_THROTTLED_MODE_REVALIDATE_SECONDS,
    AddressActionTracker,
)


class _ExpiryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.pttls: dict[str, int] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ):
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

    async def exists(self, key: str) -> int:
        return int(key in self.values)

    async def pttl(self, key: str) -> int:
        if key not in self.values:
            return -2
        return self.pttls.get(key, -1)

    async def hset(self, key: str, mapping: dict[str, object]):
        bucket = self.hashes.setdefault(key, {})
        for field, value in mapping.items():
            bucket[field] = str(value)
        return len(mapping)

    async def delete(self, *keys: str):
        for key in keys:
            self.values.pop(key, None)
            self.pttls.pop(key, None)
        return len(keys)

    async def incr(self, _key: str):
        return 1

    def advance(self, seconds: float) -> None:
        elapsed = int(seconds * 1000)
        for key, remaining in list(self.pttls.items()):
            updated = remaining - elapsed
            if updated <= 0:
                self.values.pop(key, None)
                self.pttls.pop(key, None)
            else:
                self.pttls[key] = updated


@pytest.mark.asyncio
async def test_authoritative_sustained_mode_expires_for_bounded_revalidation():
    redis = _ExpiryRedis()
    tracker = AddressActionTracker(redis, "testnet")
    address = "0x" + "77" * 20

    await tracker.record_exchange_snapshot(
        address,
        {
            "cumVlm": "100",
            "nRequestsUsed": 10100,
            "nRequestsCap": 10100,
            "nRequestsSurplus": 0,
        },
    )

    mode_key = tracker._mode_key(address)
    assert await redis.exists(mode_key) == 1
    assert await redis.pttl(mode_key) == ADDRESS_THROTTLED_MODE_REVALIDATE_SECONDS * 1000

    redis.advance(ADDRESS_THROTTLED_MODE_REVALIDATE_SECONDS)

    assert await redis.exists(mode_key) == 0
    # Once the bounded mode expires, TRAXION no longer invents stale exhaustion;
    # the next action may probe the venue and any real throttle will be observed
    # and re-established through the normal explicit-rejection path.
    assert await tracker.wait_if_backed_off(address) == 0.0
