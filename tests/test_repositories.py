"""Postgres -> Redis consistency: every commit that changes a Customer or the
blacklist must leave Redis matching Postgres, without callers doing anything."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

from redis.exceptions import ConnectionError as RedisConnectionError
from sqlmodel import Session

from src.blacklist.models import BlacklistedAccount
from src.blacklist.repository import BlacklistRepository
from src.profile.cache_sync import baseline_cache_key
from src.profile.repository import CustomerRepository
from src.settlement.models import Transaction
from src.settlement.repository import TransactionRepository


def _warm_cache(repo: CustomerRepository, fake_redis, customer_id: str) -> dict:
    """Creates the customer and fills its cached baseline via the normal read path."""
    repo.get_or_create(customer_id)
    baseline = repo.get_cached_baseline(customer_id)
    assert fake_redis.exists(baseline_cache_key(customer_id))
    return baseline


def test_customer_save_invalidates_cached_baseline(db_session: Session, fake_redis):
    """Saving through the repository drops the stale cache; the next read re-caches fresh data."""
    repo = CustomerRepository(db_session, fake_redis)
    assert _warm_cache(repo, fake_redis, "CUST_1")["risk_tier"] == "standard"

    customer = repo.get_for_update("CUST_1")
    customer.risk_tier = "elevated"
    repo.save(customer)

    assert not fake_redis.exists(baseline_cache_key("CUST_1"))
    assert repo.get_cached_baseline("CUST_1")["risk_tier"] == "elevated"
    cached = json.loads(fake_redis.get(baseline_cache_key("CUST_1")))
    assert cached["risk_tier"] == "elevated"


def test_customer_change_committed_by_another_repository_invalidates_cache(db_session: Session, fake_redis):
    """The case the hook exists for: a Customer change committed via TransactionRepository."""
    repo = CustomerRepository(db_session, fake_redis)
    _warm_cache(repo, fake_redis, "CUST_2")

    customer = repo.get_for_update("CUST_2")
    customer.risk_tier = "elevated"
    TransactionRepository(db_session).save(
        Transaction(
            transaction_reference="TXN_REPO_1",
            customer_id="CUST_2",
            amount=500000,
            destination_key="transfer:058:1234567890",
            provider="058",
            transaction_type="transfer",
            medium="app",
            occurred_at=datetime.now(timezone.utc),
        )
    )

    assert not fake_redis.exists(baseline_cache_key("CUST_2"))
    assert repo.get_cached_baseline("CUST_2")["risk_tier"] == "elevated"


def test_rolled_back_customer_change_keeps_cache(db_session: Session, fake_redis):
    """Nothing reached Postgres, so the cached baseline is still correct and stays."""
    repo = CustomerRepository(db_session, fake_redis)
    _warm_cache(repo, fake_redis, "CUST_3")

    customer = repo.get_for_update("CUST_3")
    customer.risk_tier = "elevated"
    db_session.flush()
    db_session.rollback()

    assert fake_redis.exists(baseline_cache_key("CUST_3"))
    assert repo.get_cached_baseline("CUST_3")["risk_tier"] == "standard"


def test_redis_failure_during_invalidation_keeps_postgres_commit(db_session: Session):
    """A Redis outage is logged, never raised, and never undoes the committed write."""
    broken_redis = MagicMock()
    broken_redis.get.side_effect = RedisConnectionError("down")
    broken_redis.set.side_effect = RedisConnectionError("down")
    broken_redis.delete.side_effect = RedisConnectionError("down")
    repo = CustomerRepository(db_session, broken_redis)

    customer = repo.get_or_create("CUST_4")
    customer.risk_tier = "elevated"
    repo.save(customer)  # must not raise

    broken_redis.delete.assert_called()
    db_session.expire_all()
    assert repo.get("CUST_4").risk_tier == "elevated"
    # Reads fall back to Postgres while Redis is down
    assert repo.get_cached_baseline("CUST_4")["risk_tier"] == "elevated"


def test_blacklist_add_rebuilds_redis_set(db_session: Session, fake_redis):
    repo = BlacklistRepository(db_session, fake_redis)

    repo.add(BlacklistedAccount(
        beneficiary_account="0666666666",
        beneficiary_bank_code="000015",
        reason="confirmed_fraud",
        source="analyst",
    ))

    assert fake_redis.sismember(BlacklistRepository.REDIS_KEY, "0666666666:000015")
    assert fake_redis.exists(BlacklistRepository.MARKER_KEY)
    assert repo.get_active_keys() == {"0666666666:000015"}


def test_blacklist_reads_postgres_without_redis(db_session: Session):
    repo = BlacklistRepository(db_session, redis_client=None)

    repo.add(BlacklistedAccount(
        beneficiary_account="0777777777",
        beneficiary_bank_code="058",
        reason="manual_flag",
        source="analyst",
    ))

    assert repo.get_active_keys() == {"0777777777:058"}
