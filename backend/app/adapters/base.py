from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from app.core.config import Network


ExecutionProvider = Literal['hyperliquid', 'risex']


@dataclass(frozen=True, slots=True)
class DestinationIdentity:
    """Immutable identity of one follower execution destination epoch."""

    provider: ExecutionProvider
    network: Network
    epoch_id: uuid.UUID
    account_address: str
