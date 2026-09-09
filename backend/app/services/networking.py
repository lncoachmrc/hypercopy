from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Network
from app.services.execution_destination import set_user_destination, user_destination_state


@dataclass(frozen=True, slots=True)
class UserNetworkState:
    network: Network
    started_at: datetime


async def user_network_state(db: AsyncSession, user_id) -> UserNetworkState:
    destination = await user_destination_state(db, user_id)
    return UserNetworkState(network=destination.network, started_at=destination.started_at)


async def set_user_network(db: AsyncSession, user_id, network: Network) -> UserNetworkState:
    try:
        current = await user_destination_state(db, user_id)
        provider = current.provider
    except RuntimeError as exc:
        if str(exc) != 'User has no active execution destination epoch':
            raise
        provider = 'hyperliquid'

    destination = await set_user_destination(
        db,
        user_id,
        provider=provider,
        network=network,
    )
    return UserNetworkState(network=destination.network, started_at=destination.started_at)
