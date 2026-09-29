"""RED integration contracts: userAbstraction must be cached across processes.

Each HyperliquidAdapter kept a private 300 s cache. The API builds a new adapter
per request and every service process starts cold, so every follower snapshot
paid 20 weight for ``userAbstraction`` on top of 2 for ``clearinghouseState``.
The shared Redis cache must remove that cost without ever inventing a value.
"""

from __future__ import annotations

import os
import uuid

import pytest
from redis.asyncio import Redis

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Priority
from app.core.config import settings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI Redis",
)


def _address() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _key(network: str, address: str) -> str:
    return f"hl:abstraction:{network}:{address.lower()}"


class RecordingLimiter:
    """Never blocks, records lane usage, exposes a real Redis client."""

    def __init__(self, redis) -> None:
        self._redis = redis
        self.requests: list[tuple[int, Priority]] = []

    async def reserve(self, weight: int, priority: Priority, *, timeout: float = 30):
        self.requests.append((weight, priority))

        class _Reservation:
            member = "test"
            reserved_weight = weight

        _Reservation.priority = priority  # type: ignore[attr-defined]
        return _Reservation()

    async def acquire(self, weight: int, priority: Priority, *, timeout: float = 30) -> None:
        self.requests.append((weight, priority))

    async def settle(self, *_args, **_kwargs) -> None:
        return None

    def lane_weight(self, priority: Priority) -> int:
        return sum(weight for weight, lane in self.requests if lane == priority)


class BrokenRedis:
    async def get(self, *_args, **_kwargs):
        raise ConnectionError("redis unavailable")

    async def set(self, *_args, **_kwargs):
        raise ConnectionError("redis unavailable")

    async def incr(self, *_args, **_kwargs):
        raise ConnectionError("redis unavailable")


class FakeInfo:
    """Stands in for the Hyperliquid SDK HTTP client."""

    def __init__(self, abstraction: str) -> None:
        self.abstraction = abstraction
        self.calls: list[tuple[str, str]] = []

    def post(self, _path: str, payload: dict):
        self.calls.append((payload["type"], payload["user"]))
        if payload["type"] == "userAbstraction":
            return self.abstraction
        raise AssertionError(f"unexpected info post {payload}")

    def user_state(self, address: str):
        self.calls.append(("clearinghouseState", address))
        return {
            "marginSummary": {"accountValue": "100", "totalMarginUsed": "0"},
            "withdrawable": "100",
            "assetPositions": [],
        }

    def spot_user_state(self, address: str):
        self.calls.append(("spotClearinghouseState", address))
        return {"balances": [{"coin": "USDC", "total": "100", "hold": "0"}]}

    def all_mids(self):
        self.calls.append(("allMids", ""))
        return {"BTC": "100"}

    def abstraction_reads(self) -> int:
        return sum(1 for kind, _user in self.calls if kind == "userAbstraction")


def _adapter(limiter, abstraction: str, network: str = "testnet") -> tuple[HyperliquidAdapter, FakeInfo]:
    adapter = HyperliquidAdapter(limiter, network=network)  # type: ignore[arg-type]
    info = FakeInfo(abstraction)
    adapter.info = info  # type: ignore[assignment]
    return adapter, info


def _redis() -> Redis:
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


@pytest.mark.asyncio
async def test_abstraction_is_shared_across_adapters_and_never_invented() -> None:
    client = _redis()
    try:
        shared = _address()
        broken = _address()
        invalid = _address()

        writer, writer_info = _adapter(RecordingLimiter(client), "unifiedAccount")
        first = await writer.user_abstraction(shared)

        reader, reader_info = _adapter(RecordingLimiter(client), "default")
        second = await reader.user_abstraction(shared)
        shared_ttl = await client.ttl(_key("testnet", shared))

        mainnet, mainnet_info = _adapter(RecordingLimiter(client), "default", network="mainnet")
        mainnet_value = await mainnet.user_abstraction(shared)

        await client.delete(_key("testnet", shared))
        expired, expired_info = _adapter(RecordingLimiter(client), "default")
        after_expiry = await expired.user_abstraction(shared)

        unavailable, unavailable_info = _adapter(RecordingLimiter(BrokenRedis()), "unifiedAccount")
        without_redis = await unavailable.user_abstraction(broken)

        await client.set(_key("testnet", invalid), "x" * 65, ex=600)
        guarded, guarded_info = _adapter(RecordingLimiter(client), "default")
        guarded_value = await guarded.user_abstraction(invalid)

        assert first == "unifiedAccount"
        assert writer_info.abstraction_reads() == 1

        assert second == "unifiedAccount" and reader_info.abstraction_reads() == 0, (
            "RED C2: a second adapter (another process or API request) must reuse "
            f"the shared cache; got value={second!r}, HTTP reads={reader_info.abstraction_reads()}"
        )
        assert 0 < shared_ttl <= 3600, (
            f"RED C2: the shared cache key must carry a TTL of at most 3600 s, got {shared_ttl}"
        )

        assert mainnet_value == "default" and mainnet_info.abstraction_reads() == 1, (
            "the testnet cache must never answer for mainnet"
        )
        assert after_expiry == "default" and expired_info.abstraction_reads() == 1, (
            "an expired shared entry must trigger a fresh HTTP read"
        )
        assert without_redis == "unifiedAccount" and unavailable_info.abstraction_reads() == 1, (
            "a Redis failure must fall back to the HTTP read, never to a default value"
        )
        assert guarded_value == "default" and guarded_info.abstraction_reads() == 1, (
            "a malformed cached value must be ignored in favour of the HTTP read"
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_portfolio_margin_from_shared_cache_is_still_rejected() -> None:
    client = _redis()
    try:
        address = _address()
        writer, _ = _adapter(RecordingLimiter(client), "portfolioMargin")
        cached = await writer.user_abstraction(address)

        reader, reader_info = _adapter(RecordingLimiter(client), "default")
        outcome: BaseException | None = None
        try:
            await reader.account_snapshot(address, priority=Priority.RECONCILE)
        except BaseException as exc:  # recorded, asserted below
            outcome = exc

        assert cached == "portfolioMargin"
        assert isinstance(outcome, ValueError) and "Portfolio Margin" in str(outcome), (
            "RED C2: portfolioMargin served from the shared cache must still be "
            f"rejected; got {outcome!r} after {reader_info.abstraction_reads()} HTTP reads"
        )
        assert reader_info.abstraction_reads() == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_warm_cache_keeps_follower_snapshots_cheap_on_reconcile() -> None:
    client = _redis()
    try:
        followers = [_address() for _ in range(10)]
        warmer, _ = _adapter(RecordingLimiter(client), "default")
        for address in followers:
            await warmer.user_abstraction(address)

        limiter = RecordingLimiter(client)
        cycle, cycle_info = _adapter(limiter, "default")
        await cycle.mids(priority=Priority.RECONCILE)
        for address in followers:
            await cycle.account_snapshot(address, priority=Priority.RECONCILE)

        reconcile_weight = limiter.lane_weight(Priority.RECONCILE)
        other_lanes = {lane for _weight, lane in limiter.requests if lane != Priority.RECONCILE}

        assert other_lanes == set()
        assert reconcile_weight <= 4 + 2 * len(followers), (
            "RED C2: with a warm shared cache, a fresh adapter reading "
            f"{len(followers)} follower snapshots must cost at most "
            f"{4 + 2 * len(followers)} on RECONCILE, got {reconcile_weight} "
            f"({cycle_info.abstraction_reads()} userAbstraction HTTP reads)"
        )
    finally:
        await client.aclose()
