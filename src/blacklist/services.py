from src.blacklist.models import BlacklistedAccount
from src.blacklist.repository import BlacklistRepository

class BlacklistService:
    """Blacklist operations for the rest of the app. All Postgres and Redis
    access (and keeping the two in sync) lives in BlacklistRepository."""

    def __init__(self, db_session, redis_client=None):
        self.repo = BlacklistRepository(db_session, redis_client)

    def get_active_keys(self) -> set[str]:
        return self.repo.get_active_keys()

    def add(self, entry: BlacklistedAccount) -> BlacklistedAccount:
        return self.repo.add(entry)

    def sync_to_redis(self) -> set[str]:
        """Forces a rebuild of the Redis set from Postgres (startup warm-up, CLI)."""
        return self.repo.sync_cache()
