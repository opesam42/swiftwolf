import os

import redis

from src.config import settings


def get_redis_client() -> redis.Redis:
    """Create a Redis client once at startup and reuse it across requests.

    By default this uses the configured REDIS_URL from settings, which points to
    your local Redis instance. It still supports legacy host/port/password/SSL
    environment variables so you can switch to a managed production service
    later without changing the app code.
    """
    redis_url = settings.REDIS_URL
    if redis_url:
        return redis.from_url(redis_url, decode_responses=False)

    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD")
    ssl = os.getenv("REDIS_SSL", "false").lower() == "true"

    return redis.Redis(
        host=host,
        port=port,
        password=password,
        ssl=ssl,
        decode_responses=False,  # keep bytes — CustomerProfileService already handles
                                 # decoding via json.loads(), which accepts bytes directly
    )