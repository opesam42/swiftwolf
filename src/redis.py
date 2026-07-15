"""
redis_client.py — real Redis connection setup for deployment.

Both Redis Cloud (free tier) and Upstash give you a host, port, and password
after signup — no code changes needed between them, just different env values.
"""
from config import settings
import redis


def get_redis_client() -> redis.Redis:
    """Builds the real Redis client from environment variables. Called once
    at app startup, the resulting client is reused across requests — do not
    create a new client per request."""
    return redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ.get("REDIS_PORT", 6379)),
        password=os.environ.get("REDIS_PASSWORD"),  # None locally, set in production
        ssl=os.environ.get("REDIS_SSL", "true").lower() == "true",  # both Redis Cloud and
                                                                       # Upstash require TLS
        decode_responses=False,  # keep bytes — CustomerProfileService already handles
                                   # decoding via json.loads(), which accepts bytes directly
    )