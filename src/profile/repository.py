# src/profile/repository.py

import json
import logging

from redis.exceptions import RedisError
from sqlmodel import Session, select
from src.profile.models import Customer

logger = logging.getLogger(__name__)

BASELINE_CACHE_TTL_SECONDS = 86400  # 24h

# session.info slot holding the Redis client, read by the cache_sync commit hook.
# Defined here (not in cache_sync) so imports only go cache_sync -> repository.
REDIS_CLIENT_INFO_KEY = "redis_client"

class CustomerRepository:
    """Owns all direct database and cache access for Customer rows. Services
    never touch SQLModel/SQLAlchemy or Redis directly — they only call methods here.

    ⚠️ ARCHITECTURAL WARNING: Automatic Redis cache invalidation relies on SQLAlchemy ORM session tracking. ALWAYS mutate loaded SQLModel instances (e.g., customer.field = value). DO NOT run raw SQL string updates (e.g., UPDATE customer SET ...) on the Customer table, as raw SQL bypasses ORM unit-of-work tracking and will cause stale Redis cache keys!
    """

    def __init__(self, db_session: Session, redis_client=None):
        self.db = db_session
        self.redis = redis_client
        if redis_client is not None:
            # lets the cache_sync commit hook reach Redis for this session
            self.db.info[REDIS_CLIENT_INFO_KEY] = redis_client

    @staticmethod
    def baseline_cache_key(customer_id: str) -> str:
        # Return dynamic cache key for the customer
        return f"baseline:{customer_id}"

    def get_cached_baseline(self, customer_id: str) -> dict | None:
        """Returns the customer's baseline from Redis, falling back to
        Postgres (and re-filling the cache) on a miss or Redis failure."""
        if self.redis is not None:
            try:
                raw = self.redis.get(self.baseline_cache_key(customer_id))
            except RedisError as e:
                logger.warning(f"Baseline cache read failed for {customer_id}, using Postgres: {e}")
                raw = None
            if raw:
                return json.loads(raw if isinstance(raw, str) else raw.decode())

        customer = self.get(customer_id)
        if customer is None:
            return None

        baseline = customer.to_baseline_dict()
        self._fill_cache(customer_id, baseline)
        return baseline

    def _fill_cache(self, customer_id: str, baseline: dict) -> None:
        """Read-path only. Never call this after a write — the commit hook handles that."""
        if self.redis is None:
            return
        try:
            self.redis.set(self.baseline_cache_key(customer_id), json.dumps(baseline), ex=BASELINE_CACHE_TTL_SECONDS)
        except RedisError as e:
            logger.warning(f"Baseline cache fill failed for {customer_id}: {e}")

    def get(self, customer_id: str) -> Customer | None:
        """Plain read, no lock. Returns None if the customer doesn't exist yet."""
        return self.db.exec(
            select(Customer).where(Customer.customer_id == customer_id)
        ).first()

    def get_or_create(self, customer_id: str) -> Customer:
        """Unlocked get-or-create for read paths such as /v1/score. Commits a
        newly created row straight away, so it never holds a lock — use
        get_or_create_for_update() when the row is about to be modified."""
        customer = self.get(customer_id)
        if customer is None:
            customer = self.save(Customer(customer_id=customer_id))
        return customer

    def get_for_update(self, customer_id: str) -> Customer | None:
        """Fetches a customer row and locks it for the duration of the
        current transaction. Returns None if the customer doesn't exist yet."""
        return self.db.exec(
            select(Customer)
            .where(Customer.customer_id == customer_id)
            .with_for_update()
        ).first()

    def create(self, customer_id: str) -> Customer:
        """Inserts a brand-new customer row and flushes it (so it becomes
        a real, lockable row within the current transaction) WITHOUT
        committing — the caller decides when the transaction actually ends."""
        customer = Customer(customer_id=customer_id)
        self.db.add(customer)
        self.db.flush()
        return customer

    def get_or_create_for_update(self, customer_id: str) -> Customer:
        """Combines the two above: get the locked row if it exists,
        otherwise create it. Either way, the returned object is safe
        from concurrent updates for the rest of this transaction."""
        customer = self.get_for_update(customer_id)
        if customer is None:
            customer = self.create(customer_id)
        return customer

    def save(self, customer: Customer) -> Customer:
        """Commits the current transaction and refreshes the object
        from the database. The cached baseline is invalidated by the
        cache_sync commit hook, not here."""
        self.db.add(customer)
        self.db.commit()
        self.db.refresh(customer)
        return customer