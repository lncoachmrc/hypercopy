"""Cross-process serialization for one user's position ledger.

Execution applies confirmed fills as deltas while reconciliation replaces ledger
sizes with the exchange's authoritative snapshot.  Those operations must not
overlap for the same user: a snapshot taken before a fill could otherwise commit
after the fill and erase it from the operational ledger.

The lock is transaction-scoped on a dedicated PostgreSQL connection so it stays
held across the execution service's intentional commits around an external order.
Closing, cancelling, or losing that connection releases the lock automatically.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import blake2b

from sqlalchemy import text

from app.db.session import engine


def _lock_id(user_id: uuid.UUID | str) -> int:
    scope = f"hypercopy:position-ledger:{user_id}".encode()
    return int.from_bytes(blake2b(scope, digest_size=8).digest(), "big", signed=True)


@asynccontextmanager
async def position_ledger_lock(user_id: uuid.UUID | str) -> AsyncIterator[None]:
    """Serialize all absolute and delta ledger writes for one user.

    Different users receive different advisory keys and remain independent.
    PostgreSQL owns the lock, so it also protects against another worker replica
    or process; an in-process ``asyncio.Lock`` would not.
    """

    async with engine.connect() as connection, connection.begin():
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _lock_id(user_id)},
        )
        yield
