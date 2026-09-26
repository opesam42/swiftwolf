# src/profile/repository.py

import json

from sqlmodel import Session, select
from src.profile.models import Customer

BASELINE_CACHE_TTL_SECONDS = 86400  # 24h

class CustomerRepository:
    """Owns all direct database and cache access for Customer rows. Services
    never touch SQLModel/SQLAlchemy or Redis directly — they only call methods here."""

    def __init__(self, db_session: Session, redis_client=None):
        self.db = db_session
        self.redis = redis_client

    def _redis_key(self, customer_id: str) -> str:
        return f"baseline:{customer_id}"

    def get_cached_baseline(self, customer_id: str) -> dict | None:
        """Returns customer risk profile from cache."""
        if self.redis is not None:
            raw = self.redis.get(self._redis_key(customer_id))
            if raw:
                data = json.loads(raw if isinstance(raw, str) else raw.decode())
                return data

        # Fallback to Postgres on cache miss
        customer = self.get(customer_id)
        if customer is None:
            return None

        baseline = customer.to_baseline_dict()
        self.cache_baseline(customer_id, baseline)
        return baseline

    def cache_baseline(self, customer_id: str, baseline: dict) -> None:
        if self.redis is not None:
            serializable = dict(baseline)
            self.redis.set(self._redis_key(customer_id), json.dumps(serializable), ex=BASELINE_CACHE_TTL_SECONDS)

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
        from the database."""
        self.db.add(customer)
        self.db.commit()
        self.db.refresh(customer)
        return customer