from __future__ import annotations

from app.core.config import settings
from app.core.security import normalize_address
from app.models.entities import User

MASTER_SOURCE_MODE = 'MASTER_SOURCE_READ_ONLY'
MASTER_SOURCE_NETWORK = 'mainnet'
MASTER_SOURCE_FOLLOWER_BLOCK_REASON = (
    'Configured master source is MAINNET read-only and cannot use follower controls'
)


def _normalized_address(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_address(value)
    except Exception:
        return None


def is_master_source_wallet(wallet: str | None) -> bool:
    master = _normalized_address(settings.HYPERLIQUID_MASTER_ADDRESS)
    candidate = _normalized_address(wallet)
    return bool(master and candidate and candidate == master)


def is_master_source_user(user: User) -> bool:
    return is_master_source_wallet(getattr(user, 'auth_wallet', None))


def follower_controls_enabled(user: User) -> bool:
    return not is_master_source_user(user)
