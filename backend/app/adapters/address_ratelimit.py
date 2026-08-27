"""Per-address Hyperliquid action observability and conservative backoff.

Hyperliquid applies an address/user action budget in addition to the shared IP
weight limit. The official budget is exchange-owned and depends on cumulative
traded volume, so TRAXION must not invent an authoritative client-side cap.

This module therefore does three things only:
- account locally for signed action attempts per follower account address;
- impose the documented 10-second cool-down after an observed exchange throttle;
- persist the latest authoritative ``userRateLimit`` snapshot for diagnostics.

The public follower account address is used as the key. Private keys and Agent
Wallet secrets are never stored here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

ADDRESS_BACKOFF_SECONDS = 10
_KEY_PREFIX = "hypercopy:hl:address"
_METRIC_PREFIX = "hypercopy:metrics"


def _normalize_address(address: str) -> str:
    normalized = address.strip().lower()
    if not normalized:
        raise ValueError("Hyperliquid account address is required")
    return normalized


def _decode_map(values: dict[Any, Any]) -> dict[str, str]:
    decoded: dict[str, str] = {}
    for key, value in values.items():
        k = key.decode() if isinstance(key, bytes) else str(key)
        v = value.decode() if isinstance(value, bytes) else str(value)
        decoded[k] = v
    return decoded


def is_exchange_rate_limit_error(exc: Exception) -> bool:
    """Match an exchange/network throttle without reclassifying business errors."""

    message = str(exc).lower()
    compact = message.replace("_", "").replace("-", "").replace(" ", "")
    return (
        "429" in message
        or "too many requests" in message
        or "rate limit" in message
        or "ratelimit" in compact
    )


@dataclass(frozen=True, slots=True)
class AddressRateLimitSnapshot:
    network: str
    address: str
    local_action_attempts: int
    local_throttle_count: int
    backoff_seconds_remaining: int
    last_action_at_ms: int | None
    last_throttled_at_ms: int | None
    exchange_cum_volume: str | None
    exchange_requests_used: int | None
    exchange_requests_cap: int | None
    exchange_requests_surplus: int | None
    exchange_snapshot_at_ms: int | None

    def as_dict(self) -> dict[str, object]:
        remaining = None
        if self.exchange_requests_cap is not None and self.exchange_requests_used is not None:
            remaining = max(self.exchange_requests_cap - self.exchange_requests_used, 0)
        return {
            "network": self.network,
            "address": self.address,
            "local_action_attempts": self.local_action_attempts,
            "local_throttle_count": self.local_throttle_count,
            "backoff_seconds_remaining": self.backoff_seconds_remaining,
            "last_action_at_ms": self.last_action_at_ms,
            "last_throttled_at_ms": self.last_throttled_at_ms,
            "exchange": {
                "cum_volume": self.exchange_cum_volume,
                "requests_used": self.exchange_requests_used,
                "requests_cap": self.exchange_requests_cap,
                "requests_surplus": self.exchange_requests_surplus,
                "remaining_from_reported_fields": remaining,
                "snapshot_at_ms": self.exchange_snapshot_at_ms,
            },
        }


class AddressActionTracker:
    """Redis-backed per-account action accounting shared by all app processes."""

    def __init__(self, redis: Redis, network: str) -> None:
        self._redis = redis
        self._network = str(network)

    def _state_key(self, address: str) -> str:
        return f"{_KEY_PREFIX}:{self._network}:{_normalize_address(address)}"

    def _backoff_key(self, address: str) -> str:
        return f"{self._state_key(address)}:backoff"

    async def wait_if_backed_off(self, address: str) -> float:
        """Honor an observed address throttle, including cross-process extensions."""

        waited = 0.0
        backoff_key = self._backoff_key(address)
        while True:
            remaining_ms = int(await self._redis.pttl(backoff_key) or 0)
            if remaining_ms <= 0:
                return waited
            await self._redis.incr(f"{_METRIC_PREFIX}:hl_address_backoff_wait_count")
            sleep_seconds = remaining_ms / 1000
            await asyncio.sleep(sleep_seconds)
            waited += sleep_seconds

    async def record_action_attempt(self, address: str) -> None:
        key = self._state_key(address)
        now_ms = int(time.time() * 1000)
        await self._redis.hincrby(key, "local_action_attempts", 1)
        await self._redis.hset(key, mapping={"last_action_at_ms": now_ms})
        await self._redis.incr(f"{_METRIC_PREFIX}:hl_address_action_attempt_count")

    async def mark_throttled(self, address: str) -> None:
        key = self._state_key(address)
        now_ms = int(time.time() * 1000)
        await self._redis.set(self._backoff_key(address), "1", ex=ADDRESS_BACKOFF_SECONDS)
        await self._redis.hincrby(key, "local_throttle_count", 1)
        await self._redis.hset(key, mapping={"last_throttled_at_ms": now_ms})
        await self._redis.incr(f"{_METRIC_PREFIX}:hl_address_throttle_count")

    async def record_exchange_snapshot(self, address: str, payload: dict[str, Any]) -> None:
        """Persist only documented userRateLimit fields plus observation time."""

        def _numeric(name: str) -> str:
            value = payload.get(name)
            if value in (None, ""):
                return ""
            return str(int(value))

        key = self._state_key(address)
        mapping = {
            "exchange_cum_volume": str(payload.get("cumVlm") or ""),
            "exchange_requests_used": _numeric("nRequestsUsed"),
            "exchange_requests_cap": _numeric("nRequestsCap"),
            "exchange_requests_surplus": _numeric("nRequestsSurplus"),
            "exchange_snapshot_at_ms": int(time.time() * 1000),
        }
        await self._redis.hset(key, mapping=mapping)

    async def snapshot(self, address: str) -> AddressRateLimitSnapshot:
        normalized = _normalize_address(address)
        values = _decode_map(await self._redis.hgetall(self._state_key(normalized)))
        ttl = int(await self._redis.ttl(self._backoff_key(normalized)) or 0)

        def _int(name: str) -> int | None:
            raw = values.get(name)
            if raw in (None, ""):
                return None
            try:
                return int(raw)
            except ValueError:
                return None

        return AddressRateLimitSnapshot(
            network=self._network,
            address=normalized,
            local_action_attempts=_int("local_action_attempts") or 0,
            local_throttle_count=_int("local_throttle_count") or 0,
            backoff_seconds_remaining=max(ttl, 0),
            last_action_at_ms=_int("last_action_at_ms"),
            last_throttled_at_ms=_int("last_throttled_at_ms"),
            exchange_cum_volume=values.get("exchange_cum_volume") or None,
            exchange_requests_used=_int("exchange_requests_used"),
            exchange_requests_cap=_int("exchange_requests_cap"),
            exchange_requests_surplus=_int("exchange_requests_surplus"),
            exchange_snapshot_at_ms=_int("exchange_snapshot_at_ms"),
        )
