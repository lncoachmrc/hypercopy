"""Weighted rate limiter for the Hyperliquid REST budget.

VERIFIED, from Hyperliquid's official rate-limit documentation:

    "The following rate limits apply per IP address:
     REST requests share an aggregated weight limit of 1200 per minute."

with per-endpoint weights:

    exchange actions                                     weight 1
    l2Book, allMids, clearinghouseState, orderStatus     weight 2
    userRole                                             weight 60
    all other info requests                              weight 20
    userFills et al: additional weight per 20 items returned

This is the binding constraint on the whole product. The bucket lives in Redis
because Railway replicas can share one egress IP and therefore one exchange
quota. Lanes reserve capacity for product-critical work while still respecting
the single global ceiling.

The production priority order is:

    ORDER > MASTER_STATE > RECONCILE > METADATA > DIAGNOSTIC

MASTER_STATE has a deliberately larger lane because every follower target is
anchored to a trustworthy master snapshot. Starving that lane makes both the
watcher and reconciliation unable to prove leverage/equity, which correctly
causes opening exposure to fail closed.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from enum import IntEnum

from redis.asyncio import Redis

BUCKET_KEY = "hl:bucket:rest"
WINDOW_SECONDS = 60

# VERIFIED weights.
WEIGHT_EXCHANGE_ACTION = 1
WEIGHT_CHEAP_INFO = 2       # clearinghouseState, allMids, orderStatus, l2Book
WEIGHT_STANDARD_INFO = 20   # meta, userFills, extraAgents, ...
WEIGHT_USER_ROLE = 60
# userFillsByTime: base 20 + up to 100 additional weight for 2000 returned
# items (1 additional per 20). Reserving worst-case before the request is the
# only way a client-side limiter can guarantee it does not overshoot the server.
WEIGHT_USER_FILLS_MAX = 120


class Priority(IntEnum):
    """Independent consumer lanes sharing the same global REST ceiling."""

    RECONCILE = 10      # verifying follower state: can wait a cycle
    DIAGNOSTIC = 15     # explicit operator reads; lowest operational priority
    METADATA = 20       # asset specs: cached for minutes
    MASTER_STATE = 30   # master equity/positions/leverage: drives all targets
    ORDER = 40          # follower exchange actions: never deferred first


@dataclass(frozen=True, slots=True)
class RateLimitReservation:
    """One pessimistic weighted reservation that can later be settled downward."""

    member: str
    priority: Priority
    reserved_weight: int


@dataclass(frozen=True, slots=True)
class Budget:
    """Per-consumer share of the per-minute IP budget.

    MASTER_STATE remains protected at 25% of the global budget because every
    follower target depends on a trustworthy master snapshot. RECONCILE is sized
    to 210 so one cold AI Profit Exit evaluation can complete its bounded
    follower snapshot, fills, funding and fee reads without borrowing from ORDER
    or MASTER_STATE. The extra reconciliation capacity is taken only from the
    lower-priority diagnostic and metadata lanes. The sum remains exactly 1200,
    including a 40-weight emergency reserve.
    """

    total_per_minute: int = 1200
    orders: int = 560
    reconcile: int = 210
    diagnostic: int = 20
    master_state: int = 300
    metadata: int = 70
    reserve: int = 40

    def allowance(self, priority: Priority) -> int:
        return {
            Priority.ORDER: self.orders,
            Priority.RECONCILE: self.reconcile,
            Priority.DIAGNOSTIC: self.diagnostic,
            Priority.MASTER_STATE: self.master_state,
            Priority.METADATA: self.metadata,
        }[priority]

    def lane_limits(self) -> dict[str, int]:
        """Authoritative lane capacities exposed to admin observability."""
        return {priority.name.lower(): self.allowance(priority) for priority in Priority}

    def validate(self) -> None:
        allocated = (
            self.orders + self.reconcile + self.diagnostic
            + self.master_state + self.metadata + self.reserve
        )
        if allocated > self.total_per_minute:
            raise ValueError(
                f"Allocated {allocated} exceeds the {self.total_per_minute} "
                "per-minute IP budget"
            )


class RateLimitExhausted(RuntimeError):
    """Raised when capacity could not be acquired before the caller timeout."""


class WeightedRateLimiter:
    """Sliding-window weighted limiter shared across every process."""

    def __init__(
        self,
        redis: Redis,
        budget: Budget | None = None,
        *,
        key: str = BUCKET_KEY,
    ) -> None:
        self._redis = redis
        self._budget = budget or Budget()
        self._budget.validate()
        self._key = key

    async def used(self, priority: Priority | None = None) -> int:
        """Weight consumed in the trailing window."""
        now = time.time()
        key = self._key if priority is None else f"{self._key}:{priority.name}"
        await self._redis.zremrangebyscore(key, 0, now - WINDOW_SECONDS)
        members = await self._redis.zrange(key, 0, -1)
        return sum(_weight_of(m) for m in members)

    async def headroom(self, priority: Priority) -> int:
        """Remaining weight for this consumer, respecting lane and global caps."""
        global_used = await self.used()
        lane_used = await self.used(priority)
        return max(
            0,
            min(
                self._budget.allowance(priority) - lane_used,
                self._budget.total_per_minute - self._budget.reserve - global_used,
            ),
        )

    async def try_reserve(
        self,
        weight: int,
        priority: Priority,
    ) -> RateLimitReservation | None:
        """Atomically reserve pessimistic weighted capacity.

        Variable-size Hyperliquid responses are charged by item count. Callers
        may reserve the documented worst case before the request and settle the
        reservation downward after the response size is known.
        """
        if weight <= 0:
            return RateLimitReservation(
                member="0:noop",
                priority=priority,
                reserved_weight=0,
            )
        now = time.time()
        lane_key = f"{self._key}:{priority.name}"
        member = f"{weight}:{uuid.uuid4().hex}"
        script = """
        local global_key=KEYS[1]
        local lane_key=KEYS[2]
        local cutoff=tonumber(ARGV[1])
        local now=tonumber(ARGV[2])
        local weight=tonumber(ARGV[3])
        local global_cap=tonumber(ARGV[4])
        local lane_cap=tonumber(ARGV[5])
        local reserve=tonumber(ARGV[6])
        local member=ARGV[7]
        redis.call('ZREMRANGEBYSCORE',global_key,0,cutoff)
        redis.call('ZREMRANGEBYSCORE',lane_key,0,cutoff)
        local function total(key)
          local rows=redis.call('ZRANGE',key,0,-1)
          local sum=0
          for _,v in ipairs(rows) do
            local n=string.match(v,'^(%d+):')
            if n then sum=sum+tonumber(n) end
          end
          return sum
        end
        local gu=total(global_key)
        local lu=total(lane_key)
        if gu+weight > global_cap-reserve or lu+weight > lane_cap then return 0 end
        redis.call('ZADD',global_key,now,member)
        redis.call('EXPIRE',global_key,120)
        redis.call('ZADD',lane_key,now,member)
        redis.call('EXPIRE',lane_key,120)
        return 1
        """
        result = await self._redis.eval(
            script, 2, self._key, lane_key, now-WINDOW_SECONDS, now, weight,
            self._budget.total_per_minute, self._budget.allowance(priority),
            self._budget.reserve, member,
        )
        if not result:
            return None
        return RateLimitReservation(
            member=member,
            priority=priority,
            reserved_weight=weight,
        )

    async def try_acquire(self, weight: int, priority: Priority) -> bool:
        """Atomically take weighted capacity from global and priority lanes."""
        return await self.try_reserve(weight, priority) is not None

    async def reserve(
        self,
        weight: int,
        priority: Priority,
        *,
        timeout: float = 30.0,
        poll: float = 0.25,
    ) -> RateLimitReservation:
        """Wait for headroom, then return a reservation token."""
        deadline = time.monotonic() + timeout
        while True:
            reservation = await self.try_reserve(weight, priority)
            if reservation is not None:
                return reservation
            if time.monotonic() >= deadline:
                raise RateLimitExhausted(
                    f"No headroom for weight {weight} on the "
                    f"{priority.name} lane after {timeout:g}s"
                )
            await asyncio.sleep(poll)

    async def settle(
        self,
        reservation: RateLimitReservation,
        actual_weight: int,
    ) -> None:
        """Reduce a pessimistic reservation to the exchange's actual weight.

        Settlement is downward-only. Failed/unknown requests intentionally keep
        their full reservation because their server-side accounting is unknown.
        """
        actual = int(actual_weight)
        if actual < 0 or actual > reservation.reserved_weight:
            raise ValueError(
                f"Cannot settle reserved weight {reservation.reserved_weight} "
                f"to {actual}"
            )
        if reservation.reserved_weight == 0 or actual == reservation.reserved_weight:
            return

        lane_key = f"{self._key}:{reservation.priority.name}"
        suffix = reservation.member.split(":", 1)[-1]
        replacement = f"{actual}:{suffix}"
        script = """
        local global_key=KEYS[1]
        local lane_key=KEYS[2]
        local old_member=ARGV[1]
        local new_member=ARGV[2]
        local actual=tonumber(ARGV[3])
        local global_score=redis.call('ZSCORE',global_key,old_member)
        local lane_score=redis.call('ZSCORE',lane_key,old_member)
        redis.call('ZREM',global_key,old_member)
        redis.call('ZREM',lane_key,old_member)
        if actual > 0 then
          if global_score then redis.call('ZADD',global_key,global_score,new_member) end
          if lane_score then redis.call('ZADD',lane_key,lane_score,new_member) end
        end
        return 1
        """
        await self._redis.eval(
            script,
            2,
            self._key,
            lane_key,
            reservation.member,
            replacement,
            actual,
        )

    async def acquire(
        self,
        weight: int,
        priority: Priority,
        *,
        timeout: float = 30.0,
        poll: float = 0.25,
    ) -> None:
        """Wait for headroom, then take it."""
        await self.reserve(
            weight,
            priority,
            timeout=timeout,
            poll=poll,
        )

    async def snapshot(self) -> dict[str, object]:
        """Current usage plus authoritative capacities for the admin control room."""
        global_used = await self.used()
        lanes = {p.name: await self.used(p) for p in Priority}
        return {
            "window_seconds": WINDOW_SECONDS,
            "total_budget": self._budget.total_per_minute,
            "reserve": self._budget.reserve,
            "used": global_used,
            "used_pct": round(global_used / self._budget.total_per_minute * 100, 1),
            "lane_limits": self._budget.lane_limits(),
            **{f"lane_{name.lower()}": value for name, value in lanes.items()},
        }


def _weight_of(member: str) -> int:
    try:
        return int(member.split(":", 1)[0])
    except (ValueError, IndexError):
        return 0


def followers_per_minute(budget: Budget | None = None) -> int:
    """Conservative follower-execution throughput from the order lane."""
    b = budget or Budget()
    return b.orders // (WEIGHT_EXCHANGE_ACTION + WEIGHT_CHEAP_INFO)
