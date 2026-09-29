"""RED contracts: the master watcher replay must not depend on the RECONCILE lane.

Production evidence (28-29/09): after every master websocket rotation the
watcher replays missed fills through ``user_fills_by_time``. That read reserved
120 weight on RECONCILE, which is continuously occupied by follower
reconciliation and AI Profit Exit, so ``replay()`` raised RateLimitExhausted
every ~35 s for hours and the websocket never reopened.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Priority, RateLimitExhausted
from app.workers import watcher as watcher_module
from app.workers.watcher import Watcher


class _Reservation:
    def __init__(self, weight: int, priority: Priority) -> None:
        self.member = "test"
        self.priority = priority
        self.reserved_weight = weight


class LaneLimiter:
    """Limiter double: records every request and exhausts selected lanes."""

    def __init__(self, exhausted: set[Priority] | None = None) -> None:
        self.exhausted = set(exhausted or set())
        self.requests: list[tuple[str, int, Priority]] = []
        self._redis = _NullRedis()

    async def reserve(self, weight: int, priority: Priority, *, timeout: float = 30):
        self.requests.append(("reserve", weight, priority))
        if priority in self.exhausted:
            raise RateLimitExhausted(
                f"No headroom for weight {weight} on the {priority.name} lane after {timeout}s"
            )
        return _Reservation(weight, priority)

    async def acquire(self, weight: int, priority: Priority, *, timeout: float = 30) -> None:
        self.requests.append(("acquire", weight, priority))
        if priority in self.exhausted:
            raise RateLimitExhausted(
                f"No headroom for weight {weight} on the {priority.name} lane after {timeout}s"
            )

    async def settle(self, reservation, actual_weight: int) -> None:
        self.requests.append(("settle", actual_weight, reservation.priority))


class _NullRedis:
    async def incr(self, *_args, **_kwargs):
        return 1


class FakeInfo:
    def __init__(self) -> None:
        self.fill_reads: list[tuple[str, int]] = []

    def user_fills_by_time(self, address: str, start_ms: int, end_ms=None):
        self.fill_reads.append((address, start_ms))
        return []


class FakeLease:
    def __init__(self) -> None:
        self.lost = asyncio.Event()

    async def start_renewal(self) -> None:
        return None

    async def stop_renewal(self) -> None:
        return None

    async def release(self) -> None:
        return None


@asynccontextmanager
async def _no_db():
    yield object()


def _adapter(limiter: LaneLimiter) -> tuple[HyperliquidAdapter, FakeInfo]:
    adapter = HyperliquidAdapter(limiter, network="testnet")  # type: ignore[arg-type]
    info = FakeInfo()
    adapter.info = info  # type: ignore[assignment]
    return adapter, info


@pytest.mark.asyncio
async def test_watcher_replay_survives_exhausted_reconcile_lane(monkeypatch) -> None:
    replay_limiter = LaneLimiter(exhausted={Priority.RECONCILE})
    adapter, info = _adapter(replay_limiter)

    baseline_limiter = LaneLimiter()
    baseline_adapter, _ = _adapter(baseline_limiter)

    local_stop = asyncio.Event()
    websocket_sessions: list[str] = []

    async def master_fills(_address: str, _stop):
        websocket_sessions.append("opened")
        local_stop.set()
        if False:  # pragma: no cover - makes this an async generator
            yield {}

    adapter.master_fills = master_fills  # type: ignore[method-assign]

    async def checkpoint(_db) -> int:
        return 1_700_000_000_000

    monkeypatch.setattr(watcher_module, "stop", local_stop)
    monkeypatch.setattr(watcher_module, "SessionLocal", _no_db)
    monkeypatch.setattr(watcher_module, "_checkpoint", checkpoint)
    monkeypatch.setattr(
        watcher_module.settings,
        "HYPERLIQUID_MASTER_ADDRESS",
        "0x00000000000000000000000000000000000000aa",
    )

    watcher = Watcher.__new__(Watcher)
    watcher.hl = adapter
    watcher.lease = FakeLease()

    errors: list[BaseException] = []
    try:
        await asyncio.wait_for(watcher.run_leader(), timeout=10)
    except Exception as exc:  # recorded, asserted below
        errors.append(exc)

    await baseline_adapter.user_fills_by_time("0x00000000000000000000000000000000000000bb", 0)

    replay_lanes = [
        priority for kind, _weight, priority in replay_limiter.requests if kind in {"reserve", "acquire"}
    ]
    baseline_lanes = [
        priority for kind, _weight, priority in baseline_limiter.requests if kind in {"reserve", "acquire"}
    ]

    assert baseline_lanes == [Priority.RECONCILE], (
        "baseline: user_fills_by_time without an explicit priority must keep "
        f"reserving on RECONCILE for reconcile/resolver/AI callers, got {baseline_lanes}"
    )
    assert errors == [], (
        "RED C1: with RECONCILE exhausted and MASTER_STATE free, the watcher "
        f"replay must complete; run_leader raised {errors!r}"
    )
    assert replay_lanes == [Priority.MASTER_STATE], (
        f"RED C1: the watcher replay must reserve on MASTER_STATE, got {replay_lanes}"
    )
    assert len(info.fill_reads) == 1
    assert websocket_sessions == ["opened"], (
        "RED C1: after the replay the watcher must reach the master websocket"
    )
