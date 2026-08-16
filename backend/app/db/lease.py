"""Single-writer lease for the master watcher, with fencing.

The problem this solves happens on every deploy, not in some rare failure. When
Railway rolls a service it starts the new container before stopping the old one.
For a few seconds two watchers are connected to Hyperliquid, both receiving the
same fills, both fanning out. Every reference implementation reviewed handles
this by writing "run exactly one replica" in a README, which is not a mechanism.

A lease alone is not enough either. The classic failure is a watcher that pauses
-- garbage collection, a slow query, the container being frozen during migration
-- long enough for its lease to expire and be taken by someone else, then wakes
up believing it is still the holder and writes. Checking `is_holder()` before
writing does not help: the pause can happen between the check and the write.

The fix is a fencing token: a counter that increases every time the lease
changes hands. Every write carries the token the writer believes it holds, and
the database rejects any write whose token is below the highest seen. A revived
old watcher cannot corrupt anything, because its token is stale by construction.
It does not need to know it lost.

The lease lives in PostgreSQL rather than Redis on purpose. Losing Redis must
never be able to produce two active watchers, and Redis is explicitly the
disposable half of this system.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_TTL_SECONDS = 15
DEFAULT_RENEW_SECONDS = 5


class LeaseLost(RuntimeError):
    """Raised when a holder discovers it no longer owns the lease."""


class FencedOut(RuntimeError):
    """Raised when a write is rejected because its token is stale."""


@dataclass(slots=True)
class LeaseState:
    name: str
    holder: str
    fencing_token: int
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


def replica_identity() -> str:
    """Stable-per-process, unique-per-replica identifier.

    Railway exposes RAILWAY_REPLICA_ID; falling back to hostname plus pid keeps
    local development and docker-compose working without special cases.
    """
    replica = os.getenv("RAILWAY_REPLICA_ID")
    if replica:
        return replica[:64]
    return f"{socket.gethostname()}-{os.getpid()}"[:64]


class WatcherLease:
    """Acquire, renew, and release the single-writer lease."""

    def __init__(
        self,
        session_factory,
        name: str = "master-watcher",
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        renew_seconds: int = DEFAULT_RENEW_SECONDS,
        holder: str | None = None,
    ) -> None:
        if renew_seconds >= ttl_seconds:
            raise ValueError("Renewal must be more frequent than the TTL")
        self._session_factory = session_factory
        self._name = name
        self._ttl = ttl_seconds
        self._renew = renew_seconds
        self._holder = holder or replica_identity()
        self._token: int | None = None
        self._renew_task: asyncio.Task | None = None
        self._lost = asyncio.Event()

    @property
    def token(self) -> int:
        if self._token is None:
            raise LeaseLost("Lease is not held")
        return self._token

    @property
    def holder(self) -> str:
        return self._holder

    @property
    def lost(self) -> asyncio.Event:
        """Set when renewal fails. The watcher should stop consuming."""
        return self._lost

    async def try_acquire(self) -> bool:
        """Take the lease if it is free or expired.

        A single statement does the whole thing: the WHERE clause admits the
        write only when the row is unclaimed, already ours, or past expiry, so
        two processes racing cannot both succeed.
        """
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self._ttl)

        async with self._session_factory() as db:  # type: AsyncSession
            result = await db.execute(
                text(
                    """
                    INSERT INTO watcher_lease
                        (name, holder, fencing_token, acquired_at, renewed_at, expires_at)
                    VALUES (:name, :holder, 1, :now, :now, :expires)
                    ON CONFLICT (name) DO UPDATE SET
                        holder        = EXCLUDED.holder,
                        fencing_token = watcher_lease.fencing_token + 1,
                        acquired_at   = EXCLUDED.acquired_at,
                        renewed_at    = EXCLUDED.renewed_at,
                        expires_at    = EXCLUDED.expires_at
                    WHERE watcher_lease.expires_at < :now
                       OR watcher_lease.holder = :holder
                    RETURNING fencing_token
                    """
                ),
                {"name": self._name, "holder": self._holder, "now": now, "expires": expires},
            )
            row = result.first()
            await db.commit()

        if row is None:
            return False

        previous, self._token = self._token, int(row[0])
        self._lost.clear()
        if previous != self._token:
            log.info(
                "Watcher lease acquired",
                extra={"holder": self._holder, "fencing_token": self._token},
            )
        return True

    async def renew(self) -> bool:
        """Extend the lease. Does not bump the token: we never lost it."""
        if self._token is None:
            return False
        now = datetime.now(UTC)

        async with self._session_factory() as db:  # type: AsyncSession
            result = await db.execute(
                text(
                    """
                    UPDATE watcher_lease
                       SET renewed_at = :now,
                           expires_at = :expires
                     WHERE name = :name
                       AND holder = :holder
                       AND fencing_token = :token
                    RETURNING fencing_token
                    """
                ),
                {
                    "name": self._name,
                    "holder": self._holder,
                    "token": self._token,
                    "now": now,
                    "expires": now + timedelta(seconds=self._ttl),
                },
            )
            row = result.first()
            await db.commit()

        if row is None:
            log.error(
                "Watcher lease lost",
                extra={"holder": self._holder, "fencing_token": self._token},
            )
            self._token = None
            self._lost.set()
            return False
        return True

    async def release(self) -> None:
        """Give the lease up on clean shutdown so a successor starts at once."""
        if self._token is None:
            return
        async with self._session_factory() as db:  # type: AsyncSession
            await db.execute(
                text(
                    """
                    UPDATE watcher_lease
                       SET expires_at = now()
                     WHERE name = :name AND holder = :holder
                       AND fencing_token = :token
                    """
                ),
                {"name": self._name, "holder": self._holder, "token": self._token},
            )
            await db.commit()
        log.info("Watcher lease released", extra={"holder": self._holder})
        self._token = None

    async def read(self) -> LeaseState | None:
        """Current lease, for the admin control room."""
        async with self._session_factory() as db:  # type: AsyncSession
            result = await db.execute(
                text(
                    "SELECT name, holder, fencing_token, expires_at "
                    "FROM watcher_lease WHERE name = :name"
                ),
                {"name": self._name},
            )
            row = result.first()
        if row is None:
            return None
        return LeaseState(
            name=row[0], holder=row[1], fencing_token=int(row[2]), expires_at=row[3]
        )

    async def start_renewal(self) -> None:
        if self._renew_task is None or self._renew_task.done():
            self._renew_task = asyncio.create_task(
                self._renewal_loop(), name="lease-renewal"
            )

    async def stop_renewal(self) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._renew_task
            self._renew_task = None

    async def _renewal_loop(self) -> None:
        while True:
            await asyncio.sleep(self._renew)
            try:
                if not await self.renew():
                    return
            except Exception:  # noqa: BLE001
                # A transient database blip should not surrender the lease: the
                # TTL still has headroom because renewal runs three times per
                # TTL. Only a definitive rejection sets `lost`.
                log.warning("Lease renewal failed; will retry", exc_info=True)


async def guarded_write(
    db: AsyncSession, lease: WatcherLease, statement: str, params: dict
) -> None:
    """Run a write that only the current lease holder may perform.

    The token check is part of the same statement as the write, which is what
    makes it safe: a watcher that stalled past its expiry cannot slip a write
    between a check and an update, because there is no gap to slip into.
    """
    guarded = f"""
        WITH fence AS (
            SELECT fencing_token FROM watcher_lease WHERE name = :__lease_name
        )
        {statement}
        AND (SELECT fencing_token FROM fence) <= :__fencing_token
    """
    result = await db.execute(
        text(guarded),
        {**params, "__lease_name": lease._name, "__fencing_token": lease.token},
    )
    if result.rowcount == 0:
        raise FencedOut(
            "Write rejected: a newer watcher holds the lease. "
            "This process should stop."
        )
