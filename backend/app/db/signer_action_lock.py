"""Cross-process serialization for Hyperliquid actions signed by one API wallet.

Hyperliquid nonces are tracked per signer. The official SDK generates timestamp-
based nonces inside exchange actions, so independent processes using the same API
wallet must not sign concurrently. A PostgreSQL advisory transaction lock gives
all TRAXION processes sharing the database one distributed critical section per
public signer address without storing or exposing private-key material.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import blake2b

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

# Signed actions can hold this connection for the duration of an exchange HTTP
# request. Keep these waits off the main application/session pool, as with the
# position-ledger lock, and bound the extra database concurrency per process.
signer_action_lock_engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=0,
    pool_recycle=900,
)


def signer_lock_id(signer_address: str) -> int:
    """Stable advisory-lock key derived only from the public API-wallet address."""

    normalized = signer_address.strip().lower()
    if not normalized:
        raise ValueError("Signer address is required for signed-action serialization")
    scope = f"hypercopy:hyperliquid-signer:{normalized}".encode()
    return int.from_bytes(blake2b(scope, digest_size=8).digest(), "big", signed=True)


@asynccontextmanager
async def signer_action_lock(signer_address: str) -> AsyncIterator[None]:
    """Serialize signed exchange actions for one API wallet across processes.

    Different API wallets use different advisory keys and remain concurrent.
    Database failure is intentionally fail-closed: a signed action must not be
    sent if TRAXION cannot establish the nonce-coordination critical section.
    """

    async with signer_action_lock_engine.connect() as connection, connection.begin():
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": signer_lock_id(signer_address)},
        )
        yield
