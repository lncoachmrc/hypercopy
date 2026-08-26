from __future__ import annotations

import asyncio
import time
from typing import Any

from hyperliquid.info import Info

from app.core.config import settings


# The strategy source is an external, read-only Hyperliquid MAINNET account.
# Public reporting must never depend on whether TRAXION is actively executing on
# that same wallet. Cache the read-only portfolio response briefly so many
# landing visitors do not fan out into one Hyperliquid request each.
_CACHE_TTL_SECONDS = 45.0
_cache: tuple[float, list[Any]] | None = None
_cache_lock = asyncio.Lock()
_mainnet_info = Info(
    settings.hyperliquid_url_for('mainnet'),
    skip_ws=True,
    meta={'universe': []},
    spot_meta={'universe': [], 'tokens': []},
)


async def master_mainnet_portfolio(*, force_refresh: bool = False) -> list[Any]:
    if not settings.HYPERLIQUID_MASTER_ADDRESS:
        raise RuntimeError('Master source is not configured')

    global _cache
    now = time.monotonic()
    if not force_refresh and _cache and _cache[0] > now:
        return _cache[1]

    async with _cache_lock:
        now = time.monotonic()
        if not force_refresh and _cache and _cache[0] > now:
            return _cache[1]

        payload = {
            'type': 'portfolio',
            'user': settings.HYPERLIQUID_MASTER_ADDRESS,
        }
        data = await asyncio.to_thread(_mainnet_info.post, '/info', payload)
        if not isinstance(data, list):
            raise RuntimeError('Unexpected Hyperliquid portfolio response')
        _cache = (time.monotonic() + _CACHE_TTL_SECONDS, data)
        return data
