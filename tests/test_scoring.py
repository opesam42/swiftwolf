from datetime import datetime, timezone

from sqlmodel import Session

from src.blacklist.models import BlacklistedAccount
from src.blacklist.services import BlacklistService


def test_score_clean_transaction_proceeds(client, auth_headers):
    """Verifies that a low-risk transaction returns PROCEED with a 200 OK."""
    payload = {
        "transaction_reference": "TXN_TEST_001",
        "customer_id": "CUST_100",
        "new_beneficiary": False,
        "beneficiary_account": "1234567890",
        "beneficiary_bank_code": "058",
        "amount": 5000.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transaction_type": "transfer",
        "medium": "app",
    }

    response = client.post("/v1/score", json=payload, headers=auth_headers)
    assert response.status_code == 200

    data = response.json()
    assert data["transaction_reference"] == "TXN_TEST_001"
    assert data["decision"] in ["PROCEED", "STEP_UP_LIGHT"]
    assert "blacklisted_account" not in data["reasons"]


def test_score_blacklisted_account_blocks(client, auth_headers, db_session: Session, fake_redis):
    """Verifies that an active blacklisted account is detected and immediately blocked."""
    # 1. Seed a blacklisted account in the DB
    blacklisted = BlacklistedAccount(
        beneficiary_account="0666666666",
        beneficiary_bank_code="000015",
        reason="confirmed_fraud",
        source="analyst",
    )
    db_session.add(blacklisted)
    db_session.commit()

    # 2. Warm the active blacklist cache in Redis
    BlacklistService(db_session, fake_redis).sync_to_redis()

    # 3. Attempt scoring against the blacklisted account
    payload = {
        "transaction_reference": "TXN_FRAUD_001",
        "customer_id": "CUST_999",
        "new_beneficiary": True,
        "beneficiary_account": "0666666666",
        "beneficiary_bank_code": "000015",
        "amount": 250000.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transaction_type": "transfer",
        "medium": "app",
    }

    response = client.post("/v1/score", json=payload, headers=auth_headers)
    assert response.status_code == 200

    data = response.json()
    assert data["decision"] == "BLOCK"
    assert data["score"] == 999
    assert "blacklisted_account" in data["reasons"]


def test_score_idempotency(client, auth_headers):
    """Verifies that repeated scoring requests with the same reference return cached decisions."""
    payload = {
        "transaction_reference": "TXN_DUP_001",
        "customer_id": "CUST_101",
        "new_beneficiary": False,
        "beneficiary_account": "9876543210",
        "beneficiary_bank_code": "033",
        "amount": 10000.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transaction_type": "transfer",
        "medium": "app",
    }

    # First Scoring Attempt
    res1 = client.post("/v1/score", json=payload, headers=auth_headers)
    assert res1.status_code == 200

    # Second Duplicate Attempt
    res2 = client.post("/v1/score", json=payload, headers=auth_headers)
    assert res2.status_code == 200
    assert res1.json() == res2.json()