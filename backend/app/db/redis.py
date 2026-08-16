from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import settings


@lru_cache(maxsize=1)
def redis_client() -> Redis:
    return Redis.from_url(settings.REDIS_URL, decode_responses=True, health_check_interval=30)
