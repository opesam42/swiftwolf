# src/profile/cache_sync.py
"""Keeps the Redis baseline cache in step with Postgres, whoever commits.

Every Customer insert/update/delete is recorded during flush, and once the
transaction commits, those customers' `baseline:{customer_id}` keys are
deleted. The next read (CustomerRepository.get_cached_baseline) misses and
re-caches the fresh row from Postgres.

Why a session hook instead of a call inside CustomerRepository.save():
all repositories share one Session per request, and a commit persists every
pending change in it — so a Customer modified and then committed through
TransactionRepository.save() (or admin code, or a CLI script) would otherwise
skip the cache. Listening on the Session class covers every commit.

Why delete instead of write-through: deletes are order-independent. Two
concurrent commits can reach Redis in either order and the result is the
same — an empty key that is refilled from Postgres. Write-through could leave
the older baseline cached for the full TTL.

Limits:
- The Redis client must be on `session.info["redis_client"]` (set by
  CustomerRepository). Sessions without it skip invalidation; the 24h TTL
  bounds the staleness.
- Bulk SQL (`update(Customer)...`, raw SQL) bypasses ORM events and is not seen.

⚠️ ARCHITECTURAL WARNING: Automatic Redis cache invalidation relies on SQLAlchemy ORM session tracking. ALWAYS mutate loaded SQLModel instances (e.g., customer.field = value). DO NOT run raw SQL string updates (e.g., UPDATE customer SET ...) on the Customer table, as raw SQL bypasses ORM unit-of-work tracking and will cause stale Redis cache keys!
"""

import logging

from redis.exceptions import RedisError
from sqlalchemy import event
from sqlalchemy.orm import Session

from src.profile.models import Customer
from src.profile.repository import CustomerRepository

logger = logging.getLogger(__name__)

REDIS_CLIENT_INFO_KEY = "redis_client"
_CHANGED_CUSTOMERS_INFO_KEY = "changed_customer_ids"    # a placeholder or key for sql achemy to recognize the keys that are to be deleted


def baseline_cache_key(customer_id: str) -> str:
    return CustomerRepository.baseline_cache_key(customer_id)


@event.listens_for(Session, "after_flush")
def _collect_changed_customers(session: Session, _flush_context) -> None:
    # new/dirty/deleted still describe what this flush just wrote
    changed = session.info.setdefault(_CHANGED_CUSTOMERS_INFO_KEY, set())
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, Customer):
            changed.add(obj.customer_id)


@event.listens_for(Session, "after_commit")
def _invalidate_changed_customers(session: Session) -> None:
    customer_ids = session.info.pop(_CHANGED_CUSTOMERS_INFO_KEY, set())
    redis_client = session.info.get(REDIS_CLIENT_INFO_KEY)
    if not customer_ids or redis_client is None:
        return

    try:
        keys_to_delete = []
        for cid in customer_ids:
            key = baseline_cache_key(cid)
            keys_to_delete.append(key)
        redis_client.delete(*keys_to_delete)

    except RedisError as e:
        # Postgres has already committed and must stay committed; the TTL bounds the drift
        logger.warning(f"Could not invalidate cached baselines for {sorted(customer_ids)}: {e}")


@event.listens_for(Session, "after_rollback")
def _discard_changed_customers(session: Session) -> None:
    # Nothing reached Postgres, so there is nothing to invalidate
    session.info.pop(_CHANGED_CUSTOMERS_INFO_KEY, None)
