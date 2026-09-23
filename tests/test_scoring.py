from datetime import datetime, timezone

from sqlmodel import Session

from src.blacklist.models import BlacklistedAccount
from src.blacklist.services import BlacklistService
from src.scoring.services import RuleEngine


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


def test_continuous_amount_scoring_thresholds():
    """
    Verifies that the Sigmoid Transfer Function scales risk points smoothly
    across Z-score thresholds and caps at max_score (50 pts).
    """
    engine = RuleEngine(
        amount_score_max_score=50.0,
        amount_score_steepness=1.5,
        amount_score_midpoint=3.0,
    )

    # 1. Normal variation (Z <= 1.0) -> 0 pts
    assert engine._calculate_continuous_score(z_score=0.5) == 0
    assert engine._calculate_continuous_score(z_score=1.0) == 0

    # 2. Slight stretch (Z = 2.0) -> ~10 pts
    score_z2 = engine._calculate_continuous_score(z_score=2.0)
    assert 8 <= score_z2 <= 12  # Approximately 10 pts

    # 3. Midpoint inflection (Z = 3.0) -> Exactly 25 pts (50% of max 50)
    assert engine._calculate_continuous_score(z_score=3.0) == 25

    # 4. High anomaly (Z = 4.0) -> ~42 pts
    score_z4 = engine._calculate_continuous_score(z_score=4.0)
    assert 40 <= score_z4 <= 44

    # 5. Extreme anomaly ceiling (Z >= 6.0) -> Capped at 50 pts
    assert engine._calculate_continuous_score(z_score=6.0) >= 49
    assert engine._calculate_continuous_score(z_score=15.0) == 50


    from datetime import datetime, timezone
