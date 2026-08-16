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

This is the binding constraint on the whole product, and it is the one thing
none of the reference implementations modelled. A naive fan-out spends
clearinghouseState (2) plus an order (1) per follower per master fill. With a
hundred followers that is 300 weight per fill, or roughly four master trades a
minute before the platform starts getting throttled.

Two consequences shape this module:

* The limit is per *IP*, and Railway replicas share an egress IP. A limiter
  living in process memory would let N replicas each believe they had the full
  budget. The bucket therefore lives in Redis and is shared.

* When the budget runs short, something has to lose. Orders win over
  reconciliation: executing a follower's trade matters more than verifying it,
  and the verification will happen on the next cycle anyway.

The window is a Redis sorted set holding one member per acquisition, scored by
timestamp. Trimming by score gives an exact sliding window rather than the
boundary spikes a fixed-window counter allows -- worth the small extra cost
when the penalty for overshooting is a 429 in the middle of placing a trade.
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
    """Who gets the budget when it is scarce. Higher wins."""

    RECONCILE = 10      # verifying follower state: can always wait a cycle
    METADATA = 20       # asset specs: cached for minutes anyway
    MASTER_STATE = 30   # reading the master: drives everything downstream
    ORDER = 40          # placing a follower's trade: never deferred first


@dataclass(frozen=True, slots=True)
class Budget:
    """Per-consumer share of the per-minute IP budget.

    Master state receives enough headroom for the watcher, worker reconciliation
    and occasional operator diagnostics to coexist in the same sliding minute.
    The aggregate still remains exactly within Hyperliquid's 1200/min ceiling.
    """

    total_per_minute: int = 1200
    orders: int = 680
    reconcile: int = 260
    master_state: int = 120
    metadata: int = 100
    reserve: int = 40

    def allowance(self, priority: Priority) -> int:
        return {
            Priority.ORDER: self.orders,
            Priority.RECONCILE: self.reconcile,
            Priority.MASTER_STATE: self.master_state,
            Priority.METADATA: self.metadata,
        }[priority]

    def validate(self) -> None:
        allocated = (
            self.orders + self.reconcile + self.master_state
            + self.metadata + self.reserve
        )
        if allocated > self.total_per_minute:
            raise ValueError(
                f"Allocated {allocated} exceeds the {self.total_per_minute} "
                "per-minute IP budget"
            )


class RateLimitExhausted(RuntimeError):
    """Raised when the caller asked not to wait and there was no headroom."""


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
        """Remaining weight for this consumer, respecting both its own share
        and the global ceiling."""
        global_used = await self.used()
        lane_used = await self.used(priority)
        return max(
            0,
            min(
                self._budget.allowance(priority) - lane_used,
                self._budget.total_per_minute - self._budget.reserve - global_used,
            ),
        )

    async def try_acquire(self, weight: int, priority: Priority) -> bool:
        """Atomically take weighted capacity from global and priority lanes."""
        if weight <= 0:
            return True
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
        return bool(result)

    async def acquire(
        self,
        weight: int,
        priority: Priority,
        *,
        timeout: float = 30.0,
        poll: float = 0.25,
    ) -> None:
        """Wait for headroom, then take it.

        Callers on the order path should pass a short timeout: a trade held for
        thirty seconds is usually worse than a trade not placed, because the
        price that justified it is gone. Reconciliation can afford to wait.
        """
        deadline = time.monotonic() + timeout
        while True:
            if await self.try_acquire(weight, priority):
                return
            if time.monotonic() >= deadline:
                raise RateLimitExhausted(
                    f"No headroom for weight {weight} on the "
                    f"{priority.name} lane after {timeout:g}s"
                )
            await asyncio.sleep(poll)

    async def snapshot(self) -> dict[str, int | float]:
        """For the admin control room: what the budget looks like right now."""
        global_used = await self.used()
        lanes = {p.name: await self.used(p) for p in Priority}
        return {
            "window_seconds": WINDOW_SECONDS,
            "total_budget": self._budget.total_per_minute,
            "used": global_used,
            "used_pct": round(global_used / self._budget.total_per_minute * 100, 1),
            **{f"lane_{name.lower()}": value for name, value in lanes.items()},
        }


def _weight_of(member: str) -> int:
    try:
        return int(member.split(":", 1)[0])
    except (ValueError, IndexError):
        return 0


def followers_per_minute(budget: Budget | None = None) -> int:
    """How many follower executions per minute the IP budget allows.

    Useful as a sanity check before promising capacity: each execution costs
    one order (weight 1) plus, in the worst case where the equity cache misses,
    one clearinghouseState (weight 2).
    """
    b = budget or Budget()
    return b.orders // (WEIGHT_EXCHANGE_ACTION + WEIGHT_CHEAP_INFO)
