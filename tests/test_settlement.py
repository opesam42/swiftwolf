from datetime import datetime, timezone

from sqlmodel import Session, select

from src.core.config import settings
from src.profile.cache_sync import baseline_cache_key
from src.profile.models import Customer
from src.scoring.services import DECISION_TO_STATUS


def _score(client, auth_headers, transaction_reference: str, customer_id: str = "CUST_100") -> dict:
    """Scores a transaction first — settlement only accepts references SwiftWolf has scored."""
    payload = {
        "transaction_reference": transaction_reference,
        "customer_id": customer_id,
        "new_beneficiary": False,
        "recipient": "1234567890",
        "provider": "058",
        "amount": 500000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transaction_type": "TRANSFER",
        "medium": "app",
    }
    response = client.post("/v1/score", json=payload, headers=auth_headers)
    assert response.status_code == 200
    return response.json()


def _settle_payload(transaction_reference: str, status: str = "SUCCESS") -> dict:
    return {
        "transaction_reference": transaction_reference,
        "customer_id": "CUST_100",
        "amount": 500000,
        "transaction_type": "TRANSFER",
        "medium": "app",
        "settled_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }


def _transfer_baseline_count(db_session: Session, customer_id: str) -> int:
    """How many settled transfers the customer's baseline has learned from."""
    db_session.expire_all()
    customer = db_session.exec(select(Customer).where(Customer.customer_id == customer_id)).one()
    return (customer.category_baselines.get("TRANSFER") or {}).get("count", 0)


def test_settle_unscored_transaction_returns_404(client, auth_headers):
    """A reference SwiftWolf never scored is rejected rather than fabricated."""
    response = client.post("/v1/transactions/settle", json=_settle_payload("TXN_NEVER_SCORED"), headers=auth_headers)
    assert response.status_code == 404


def test_settle_transaction_success(client, auth_headers, db_session):
    """Settling a scored transaction marks it settled and teaches the baseline once."""
    scored = _score(client, auth_headers, "TXN_SETTLE_001")

    response = client.post("/v1/transactions/settle", json=_settle_payload("TXN_SETTLE_001"), headers=auth_headers)
    assert response.status_code == 200

    data = response.json()
    assert data["is_settled"] is True
    # status keeps the scoring outcome; settlement is carried by is_settled
    assert data["status"] == DECISION_TO_STATUS[scored["decision"]].value
    assert _transfer_baseline_count(db_session, "CUST_100") == 1


def test_settle_transaction_idempotency_guard(client, auth_headers, db_session):
    """A retried settle call is acknowledged but never updates the baseline twice."""
    _score(client, auth_headers, "TXN_SETTLE_DUP")
    payload = _settle_payload("TXN_SETTLE_DUP")

    res1 = client.post("/v1/transactions/settle", json=payload, headers=auth_headers)
    assert res1.status_code == 200
    assert res1.json()["message"] == "Transaction successfully settled."

    res2 = client.post("/v1/transactions/settle", json=payload, headers=auth_headers)
    assert res2.status_code == 200
    assert res2.json()["is_settled"] is True
    assert res2.json()["message"] == "Transaction was previously settled."

    assert _transfer_baseline_count(db_session, "CUST_100") == 1


def test_cold_start_ends_after_min_settled_transactions(client, auth_headers, db_session, fake_redis):
    """The Nth settlement flips is_cold_start in the same commit, and the cached
    baseline is invalidated so scoring doesn't keep reading the stale flag."""
    threshold = settings.COLD_START_MIN_SETTLED_TRANSACTIONS

    for i in range(threshold):
        _score(client, auth_headers, f"TXN_COLD_{i}")
        # From the 2nd score on, scoring re-caches the baseline (the 1st score creates
        # the customer, and that insert's commit leaves nothing cached); settling must invalidate it
        if i > 0:
            assert fake_redis.exists(baseline_cache_key("CUST_100"))
        client.post("/v1/transactions/settle", json=_settle_payload(f"TXN_COLD_{i}"), headers=auth_headers)
        assert not fake_redis.exists(baseline_cache_key("CUST_100"))

        db_session.expire_all()
        customer = db_session.exec(select(Customer).where(Customer.customer_id == "CUST_100")).one()
        assert customer.is_cold_start is (i + 1 < threshold)


def test_settle_failed_transaction_leaves_baseline_untouched(client, auth_headers, db_session):
    """A FAILED settlement is recorded, but no money moved so nothing is learned."""
    _score(client, auth_headers, "TXN_SETTLE_FAIL")

    response = client.post(
        "/v1/transactions/settle", json=_settle_payload("TXN_SETTLE_FAIL", status="FAILED"), headers=auth_headers
    )
    assert response.status_code == 200

    data = response.json()
    assert data["is_settled"] is False
    assert data["status"] == "FAILED"
    assert _transfer_baseline_count(db_session, "CUST_100") == 0
