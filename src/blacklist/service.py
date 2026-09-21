from sqlmodel import select
from src.blacklist.models import BlacklistedAccount

class BlacklistService:
    REDIS_KEY = "blacklist:active_accounts"
    MARKER_KEY = "blacklist:last_synced" # Signals that a sync occurred

    def __init__(self, db_session, redis_client=None):
        self.db = db_session
        self.redis = redis_client

    def get_active_keys(self) -> set[str]:
        # check if key exist in Redis
        if self.redis.exists(self.MARKER_KEY):
            cached = self.redis.smembers(self.REDIS_KEY)
            return {m.decode() if isinstance(m, bytes) else m for m in cached}

        return self.sync_to_redis()

    def sync_to_redis(self, keys: set[str] | None = None) -> set[str]:
        """ Ensure Redis is in sync with Postgres """
        if keys is None:
            keys = self._load_active_keys_from_postgres()

        # delete the existing set and add the new keys
        # make it atomic
        pipe = self.redis.pipeline(transaction=True)
        pipe.delete(self.REDIS_KEY)

        if keys:
            pipe.sadd(self.REDIS_KEY, *keys)

        #  Unconditionally set the marker key so empty sets are recognized as synced 
        pipe.set(self.MARKER_KEY, "1")
        pipe.execute()

        return keys

    def _load_active_keys_from_postgres(self) -> list[str]:
        stmt = select(
            BlacklistedAccount.beneficiary_account,
            BlacklistedAccount.beneficiary_bank_code,
        ). where(BlacklistedAccount.is_active == True)
        rows = self.db.exec(stmt).all()

        return [f"{row.beneficiary_account}:{row.beneficiary_bank_code}" for row in rows]
