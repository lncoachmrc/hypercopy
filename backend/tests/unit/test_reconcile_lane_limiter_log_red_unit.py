"""RED contracts: limiter exhaustion inside Hyperliquid reads must be visible.

RateLimitExhausted is raised by the shared limiter before the guarded SDK call,
so today it bypasses every log and metric in ``HyperliquidAdapter._read``. The
only trace in production is the caller's generic failure message.
"""

from __future__ import annotations

import pytest

from app.adapters import hyperliquid as hyperliquid_module
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Priority, RateLimitExhausted


class RecordingRedis:
    def __init__(self) -> None:
        self.incr_keys: list[str] = []

    async def incr(self, key: str, *_args, **_kwargs) -> int:
        self.incr_keys.append(key)
        return 1


class ExhaustedLimiter:
    def __init__(self) -> None:
        self._redis = RecordingRedis()
        self.requests: list[tuple[str, int, Priority]] = []

    async def reserve(self, weight: int, priority: Priority, *, timeout: float = 30):
        self.requests.append(("reserve", weight, priority))
        raise RateLimitExhausted(
            f"No headroom for weight {weight} on the {priority.name} lane after {timeout}s"
        )

    async def acquire(self, weight: int, priority: Priority, *, timeout: float = 30) -> None:
        self.requests.append(("acquire", weight, priority))
        raise RateLimitExhausted(
            f"No headroom for weight {weight} on the {priority.name} lane after {timeout}s"
        )

    async def settle(self, *_args, **_kwargs) -> None:
        raise AssertionError("settle must not run after a failed reservation")


class RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def _record(self, level: str, message: str, *args, **kwargs) -> None:
        self.records.append((level, message, dict(kwargs.get("extra") or {})))

    def warning(self, message: str, *args, **kwargs) -> None:
        self._record("warning", message, *args, **kwargs)

    def info(self, message: str, *args, **kwargs) -> None:
        self._record("info", message, *args, **kwargs)

    def error(self, message: str, *args, **kwargs) -> None:
        self._record("error", message, *args, **kwargs)

    def exception(self, message: str, *args, **kwargs) -> None:
        self._record("exception", message, *args, **kwargs)

    def debug(self, message: str, *args, **kwargs) -> None:
        self._record("debug", message, *args, **kwargs)


class ExplodingInfo:
    def __getattr__(self, name: str):
        def _call(*_args, **_kwargs):
            raise AssertionError(f"SDK call {name} must not run without limiter capacity")

        return _call


async def _exhaust(read) -> BaseException | None:
    try:
        await read()
    except BaseException as exc:  # recorded, asserted below
        return exc
    return None


@pytest.mark.asyncio
async def test_limiter_exhaustion_is_logged_counted_and_reraised(monkeypatch) -> None:
    recording_log = RecordingLog()
    monkeypatch.setattr(hyperliquid_module, "log", recording_log)
    monkeypatch.setattr(hyperliquid_module.settings, "HL_SAFE_READ_RETRIES", 3)

    reserve_limiter = ExhaustedLimiter()
    reserve_adapter = HyperliquidAdapter(reserve_limiter, network="testnet")  # type: ignore[arg-type]
    reserve_adapter.info = ExplodingInfo()  # type: ignore[assignment]

    acquire_limiter = ExhaustedLimiter()
    acquire_adapter = HyperliquidAdapter(acquire_limiter, network="testnet")  # type: ignore[arg-type]
    acquire_adapter.info = ExplodingInfo()  # type: ignore[assignment]

    reserve_cooldown_before = reserve_adapter._read_cooldown_until
    acquire_cooldown_before = acquire_adapter._read_cooldown_until

    reserve_error = await _exhaust(
        lambda: reserve_adapter.user_fills_by_time("0x00000000000000000000000000000000000000cc", 0)
    )
    acquire_error = await _exhaust(
        lambda: acquire_adapter.user_state("0x00000000000000000000000000000000000000dd")
    )

    exhausted_logs = [
        extra for level, _message, extra in recording_log.records
        if extra.get("event_code") == "HL_LIMITER_EXHAUSTED"
    ]

    assert isinstance(reserve_error, RateLimitExhausted), reserve_error
    assert isinstance(acquire_error, RateLimitExhausted), acquire_error
    assert len(reserve_limiter.requests) == 1, (
        f"limiter exhaustion must not be retried: {reserve_limiter.requests}"
    )
    assert len(acquire_limiter.requests) == 1, (
        f"limiter exhaustion must not be retried: {acquire_limiter.requests}"
    )
    assert reserve_adapter._read_cooldown_until == reserve_cooldown_before
    assert acquire_adapter._read_cooldown_until == acquire_cooldown_before

    assert len(exhausted_logs) == 2, (
        "RED C4: every limiter exhaustion must log event_code HL_LIMITER_EXHAUSTED, "
        f"got records {recording_log.records}"
    )
    assert {log["lane"] for log in exhausted_logs} == {"RECONCILE"}
    assert sorted(int(log["weight"]) for log in exhausted_logs) == [2, 120]
    assert all(log.get("network") == "testnet" for log in exhausted_logs)
    assert all("timeout" in log for log in exhausted_logs)

    for limiter in (reserve_limiter, acquire_limiter):
        keys = limiter._redis.incr_keys
        assert "hypercopy:metrics:hl_limiter_exhausted_count" in keys, (
            f"RED C4: global exhaustion metric missing, got {keys}"
        )
        assert "hypercopy:metrics:hl_limiter_exhausted_count:RECONCILE" in keys, (
            f"RED C4: per-lane exhaustion metric missing, got {keys}"
        )
