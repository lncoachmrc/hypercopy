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
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

# Advisory-lock waits must not consume the bounded connection pool used by API
# requests and worker sessions.  A small, separate pool lets waiting lock calls
# make bounded progress without allowing an unbounded database connection burst.
position_ledger_lock_engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=0,
    pool_recycle=900,
)


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

    async with position_ledger_lock_engine.connect() as connection, connection.begin():
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _lock_id(user_id)},
        )
        yield
