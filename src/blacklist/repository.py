# src/blacklist/repository.py

import logging

from redis.exceptions import RedisError
from sqlmodel import Session, select

from src.blacklist.models import BlacklistedAccount

logger = logging.getLogger(__name__)


class BlacklistRepository:
    """Owns all Postgres and Redis access for the blacklist.

    Every write goes through add(), which commits and then rebuilds the Redis
    set — so the cache can't be forgotten. A full rebuild (rather than SADD of
    the one new key) is order-independent: concurrent writers all end up
    writing the latest Postgres state.

    Redis Cache Specification:
    - REDIS_KEY ("blacklist:active_accounts"): Redis Set of "{account}:{bank_code}"
      strings; source of truth is blacklisted_accounts where is_active = true.
      No TTL — rebuilt on every write.
    - MARKER_KEY ("blacklist:last_synced"): "1" once a sync has happened, so an
      empty blacklist can be told apart from an unsynced/flushed cache.
    """

    REDIS_KEY = "blacklist:active_accounts"
    MARKER_KEY = "blacklist:last_synced"

    def __init__(self, db_session: Session, redis_client=None):
        self.db = db_session
        self.redis = redis_client

    def add(self, entry: BlacklistedAccount) -> BlacklistedAccount:
        """Persists a blacklist entry, then rebuilds the Redis set."""
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        self.sync_cache()
        return entry

    def get_active_keys(self) -> set[str]:
        """Hot-path read: Redis when synced, otherwise Postgres (and re-sync)."""
        if self.redis is not None:
            try:
                if self.redis.exists(self.MARKER_KEY):
                    cached = self.redis.smembers(self.REDIS_KEY)
                    return {m.decode() if isinstance(m, bytes) else m for m in cached}
            except RedisError as e:
                logger.warning(f"Blacklist cache read failed, using Postgres: {e}")
                return self.load_active_keys()

        return self.sync_cache()

    def sync_cache(self) -> set[str]:
        """Rebuilds the Redis set from Postgres atomically and returns the active keys."""
        keys = self.load_active_keys()
        if self.redis is None:
            return keys

        try:
            pipe = self.redis.pipeline(transaction=True)
            pipe.delete(self.REDIS_KEY)
            if keys:
                pipe.sadd(self.REDIS_KEY, *keys)
            # Set unconditionally so an empty blacklist still counts as synced
            pipe.set(self.MARKER_KEY, "1")
            pipe.execute()
        except RedisError as e:
            # Postgres is the source of truth and is already committed
            logger.warning(f"Blacklist cache rebuild failed: {e}")

        return keys

    def load_active_keys(self) -> set[str]:
        rows = self.db.exec(
            select(
                BlacklistedAccount.beneficiary_account,
                BlacklistedAccount.beneficiary_bank_code,
            ).where(BlacklistedAccount.is_active == True)
        ).all()
        return {f"{row.beneficiary_account}:{row.beneficiary_bank_code}" for row in rows}
