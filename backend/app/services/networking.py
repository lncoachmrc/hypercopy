from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Network, settings


@dataclass(frozen=True, slots=True)
class UserNetworkState:
    network: Network
    started_at: datetime


async def user_network_state(db: AsyncSession, user_id) -> UserNetworkState:
    row = (await db.execute(
        text("SELECT execution_network, network_started_at FROM users WHERE id = :user_id"),
        {"user_id": user_id},
    )).one_or_none()
    if not row:
        raise RuntimeError("User network state is unavailable")
    raw = str(row[0] or settings.follower_network).lower()
    if raw not in {"testnet", "mainnet"}:
        raise RuntimeError(f"Unsupported user execution network: {raw}")
    started_at = row[1] or datetime.now(UTC)
    return UserNetworkState(network=raw, started_at=started_at)  # type: ignore[arg-type]


async def set_user_network(db: AsyncSession, user_id, network: Network) -> UserNetworkState:
    now = datetime.now(UTC)
    await db.execute(
        text("UPDATE users SET execution_network = :network, network_started_at = :started_at, updated_at = :started_at WHERE id = :user_id"),
        {"network": network, "started_at": now, "user_id": user_id},
    )
    return UserNetworkState(network=network, started_at=now)
