from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import settings


@lru_cache(maxsize=1)
def redis_client() -> Redis:
    # Bound network waits so an unavailable Redis cannot leave API requests or
    # workers hanging forever. The read timeout stays above the websocket
    # pubsub heartbeat interval (15s) and normal XREADGROUP blocking window.
    return Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        health_check_interval=30,
        socket_connect_timeout=5,
        socket_timeout=20,
    )
