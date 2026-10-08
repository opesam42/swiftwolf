import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
import redis
from sqlmodel import Session, select

from src.core.config import settings
from src.profile.services import CustomerProfileService
from src.scoring.models import RiskEvent, RiskReason
from src.scoring.services import RuleEngine, VelocityWindow
from src.scoring.utils import pseudonymize
from src.settlement.models import Transaction


def test_score_clean_transaction_proceeds(client, auth_headers):
    """Verifies that a low-risk transaction returns PROCEED with a 200 OK."""
    payload = {
        "transaction_reference": "TXN_TEST_001",
        "customer_id": "CUST_100",
        "recipient": "1234567890",
        "provider": "058",
        "amount": 500000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transaction_type": "transfer",
        "medium": "app",
    }

    response = client.post("/v1/score", json=payload, headers=auth_headers)
    assert response.status_code == 200

    data = response.json()
    assert data["transaction_reference"] == "TXN_TEST_001"
    # A first-time customer's first transfer is a new recipient at a new bank (+30),
    # so it may sit on the PROCEED/STEP_UP line — but a clean transaction is never blocked
    assert data["decision"] in ["PROCEED", "STEP_UP"]
    assert "blacklisted_account" not in data["reasons"]


def test_score_persists_behavioural_biometrics(client, auth_headers, db_session: Session):
    """The optional biometrics payload reaches the risk-event telemetry column."""
    telemetry = {
        "dwell_time_ms": 120.5,
        "flight_time_ms": 85.0,
        "time_to_first_keystroke_ms": 340.0,
        "backspace_count": 2.0,
    }
    payload = {
        "transaction_reference": "TXN_BIOMETRICS_001",
        "customer_id": "CUST_BIOMETRICS",
        "recipient": "1234567890",
        "provider": "058",
        "amount": 500000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transaction_type": "transfer",
        "medium": "app",
        "behavioural_biometrics": telemetry,
    }

    response = client.post("/v1/score", json=payload, headers=auth_headers)

    assert response.status_code == 200
    risk_event = db_session.exec(
        select(RiskEvent).where(
            RiskEvent.transaction_reference == "TXN_BIOMETRICS_001"
        )
    ).one()
    assert risk_event.telemetry == telemetry


def test_score_persists_stable_pseudonymized_recipient(client, auth_headers, db_session: Session):
    """Scoring stores only the stable recipient pseudonym in the destination key."""
    recipient = "0123456789"
    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "transaction_reference": "TXN_HASH_001",
        "customer_id": "CUST_HASH",
        "recipient": recipient,
        "provider": "058",
        "amount": 500000,
        "timestamp": timestamp,
        "transaction_type": "transfer",
        "medium": "app",
    }

    first_response = client.post("/v1/score", json=payload, headers=auth_headers)
    second_payload = {**payload, "transaction_reference": "TXN_HASH_002"}
    second_response = client.post("/v1/score", json=second_payload, headers=auth_headers)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    transactions = db_session.exec(
        select(Transaction).where(
            Transaction.transaction_reference.in_(["TXN_HASH_001", "TXN_HASH_002"])
        )
    ).all()
    assert len(transactions) == 2

    expected_key = f"transfer:058:{pseudonymize(recipient)}"
    assert {transaction.destination_key for transaction in transactions} == {expected_key}
    assert all(recipient not in transaction.destination_key for transaction in transactions)


def test_score_idempotency(client, auth_headers):
    """Verifies that repeated scoring requests with the same reference return cached decisions."""
    payload = {
        "transaction_reference": "TXN_DUP_001",
        "customer_id": "CUST_101",
        "recipient": "9876543210",
        "provider": "033",
        "amount": 1000000,
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


def _known_transfer(amount_kobo: int) -> dict:
    return {
        "transaction_type": "transfer",
        "provider": "058",
        "recipient": "1234567890",
        "amount": amount_kobo,
        "timestamp": datetime.now(timezone.utc),
    }


def _known_baseline(transfer_stats: dict) -> dict:
    dest = CustomerProfileService.destination_key_for("transfer", "058", "1234567890")
    return {
        "known_destinations": [dest],
        "known_bank_codes": ["058"],
        "category_baselines": {"transfer": transfer_stats},
        "is_cold_start": False,
    }


def test_amount_deviation_uses_ewma_not_lifetime_mean():
    """A ₦5,000 transfer is typical of the lifetime mean but extreme vs recent habit."""
    engine = RuleEngine()
    txn = _known_transfer(amount_kobo=500000)
    baseline = _known_baseline(
        {
            "count": 50,
            "avg_amount": 5000.0,
            "std_amount": 100.0,
            "ewma_avg": 300.0,
            "ewma_std": 50.0,
        }
    )

    result = engine.evaluate(txn, baseline)

    assert RiskReason.AMOUNT_DEVIATION in result["reasons"]
    assert result["score"] == 50


def test_amount_deviation_falls_back_to_lifetime_when_ewma_std_is_zero():
    """Baselines written before ewma_* still score against Welford."""
    engine = RuleEngine()
    txn = _known_transfer(amount_kobo=500000)
    baseline = _known_baseline(
        {
            "count": 50,
            "avg_amount": 300.0,
            "std_amount": 50.0,
        }
    )

    result = engine.evaluate(txn, baseline)

    assert RiskReason.AMOUNT_DEVIATION in result["reasons"]
    assert result["score"] == 50


def test_amount_deviation_skipped_when_ewma_matches_the_amount():
    """Lifetime mean is far away; recent EWMA says this amount is normal."""
    engine = RuleEngine()
    txn = _known_transfer(amount_kobo=500000)
    baseline = _known_baseline(
        {
            "count": 50,
            "avg_amount": 300.0,
            "std_amount": 50.0,
            "ewma_avg": 5000.0,
            "ewma_std": 100.0,
        }
    )

    result = engine.evaluate(txn, baseline)

    assert RiskReason.AMOUNT_DEVIATION not in result["reasons"]


def _biometrics(*, flight_time_ms: float = 500.0) -> dict:
    return {
        "dwell_time_ms": 120.5,
        "flight_time_ms": flight_time_ms,
        "time_to_first_keystroke_ms": 340.0,
        "backspace_count": 2.0,
    }


def _full_flight_clusters() -> list[dict]:
    return [
        {"id": i + 1, "ewma_avg": 80.0 + i * 10, "ewma_var": 25.0, "ewma_std": 5.0, "sample_count": 10}
        for i in range(5)
    ]


def test_typing_deviation_fires_when_past_cold_start_and_slots_full():
    engine = RuleEngine()
    txn = {**_known_transfer(500000), "behavioural_biometrics": _biometrics(flight_time_ms=500.0)}
    baseline = _known_baseline({"count": 50, "avg_amount": 5000.0, "std_amount": 100.0, "ewma_avg": 5000.0, "ewma_std": 100.0})
    baseline["typing_baselines"] = {
        "sample_count": 30,
        "fields": {"flight_time_ms": _full_flight_clusters()},
    }

    result = engine.evaluate(txn, baseline)

    assert RiskReason.TYPING_DEVIATION in result["reasons"]
    assert result["score"] >= 25


def test_typing_deviation_skipped_during_cold_start():
    engine = RuleEngine()
    txn = {**_known_transfer(500000), "behavioural_biometrics": _biometrics(flight_time_ms=500.0)}
    baseline = _known_baseline({"count": 50, "avg_amount": 5000.0, "std_amount": 100.0, "ewma_avg": 5000.0, "ewma_std": 100.0})
    baseline["typing_baselines"] = {
        "sample_count": 10,
        "fields": {"flight_time_ms": _full_flight_clusters()},
    }

    result = engine.evaluate(txn, baseline)

    assert RiskReason.TYPING_DEVIATION not in result["reasons"]


def test_typing_deviation_skipped_when_slots_remain():
    engine = RuleEngine()
    txn = {**_known_transfer(500000), "behavioural_biometrics": _biometrics(flight_time_ms=500.0)}
    baseline = _known_baseline({"count": 50, "avg_amount": 5000.0, "std_amount": 100.0, "ewma_avg": 5000.0, "ewma_std": 100.0})
    baseline["typing_baselines"] = {
        "sample_count": 30,
        "fields": {"flight_time_ms": _full_flight_clusters()[:2]},
    }

    result = engine.evaluate(txn, baseline)

    assert RiskReason.TYPING_DEVIATION not in result["reasons"]


# --- Velocity window ---

WINDOW_SECONDS = 600
MAX_TX_THRESHOLD = 5
PENALTY_SCORE = 50


@pytest.fixture(name="velocity")
def velocity_fixture(fake_redis):
    """Explicit window settings so the tests don't depend on .env values."""
    return VelocityWindow(
        fake_redis,
        window_seconds=WINDOW_SECONDS,
        max_tx_threshold=MAX_TX_THRESHOLD,
        penalty_score=PENALTY_SCORE,
    )


def _seed(fake_redis, customer_id: str, refs: list[str], seconds_ago: int):
    """Writes past transactions straight into the ZSET with backdated scores."""
    timestamp = time.time() - seconds_ago
    fake_redis.zadd(f"velocity:{customer_id}", {ref: timestamp for ref in refs})


def test_velocity_cold_start(velocity, fake_redis):
    """First transaction for a customer creates the key with a TTL and no penalty."""
    result = velocity.record_and_check_velocity("CUST_NEW", "TXN_1")

    assert result.tx_count == 1
    assert result.is_velocity_anomaly is False
    assert result.velocity_risk_points == 0
    assert 0 < fake_redis.ttl("velocity:CUST_NEW") <= WINDOW_SECONDS + 60


def test_velocity_below_threshold(velocity):
    """Three transactions inside the window stay under the threshold of 5."""
    for i in range(3):
        result = velocity.record_and_check_velocity("CUST_1", f"TXN_{i}")

    assert result.tx_count == 3
    assert result.is_velocity_anomaly is False
    assert result.velocity_risk_points == 0


@pytest.mark.parametrize(
    "tx_total, expected_anomaly, expected_points",
    [(4, False, 0), (5, True, PENALTY_SCORE)],
)
def test_velocity_threshold_boundary(velocity, tx_total, expected_anomaly, expected_points):
    """The anomaly fires when the count reaches the threshold, not one before."""
    for i in range(tx_total):
        result = velocity.record_and_check_velocity("CUST_1", f"TXN_{i}")

    assert result.tx_count == tx_total
    assert result.is_velocity_anomaly is expected_anomaly
    assert result.velocity_risk_points == expected_points


def test_velocity_all_expired(velocity, fake_redis):
    """Transactions older than the window are pruned before counting."""
    _seed(fake_redis, "CUST_1", [f"OLD_{i}" for i in range(4)], seconds_ago=900)

    result = velocity.record_and_check_velocity("CUST_1", "TXN_NEW")

    assert result.tx_count == 1
    assert result.is_velocity_anomaly is False
    assert fake_redis.zcard("velocity:CUST_1") == 1


def test_velocity_partial_expiry(velocity, fake_redis):
    """Only entries outside the sliding window are removed."""
    _seed(fake_redis, "CUST_1", ["OLD_1", "OLD_2"], seconds_ago=720)
    _seed(fake_redis, "CUST_1", ["RECENT_1", "RECENT_2"], seconds_ago=180)

    result = velocity.record_and_check_velocity("CUST_1", "TXN_NEW")

    assert result.tx_count == 3
    assert fake_redis.zscore("velocity:CUST_1", "OLD_1") is None
    assert fake_redis.zscore("velocity:CUST_1", "OLD_2") is None
    assert fake_redis.zscore("velocity:CUST_1", "RECENT_1") is not None


def test_velocity_retry_same_reference(velocity, fake_redis):
    """A retried tx_reference refreshes its timestamp instead of adding a duplicate."""
    _seed(fake_redis, "CUST_1", ["TXN_RETRY"], seconds_ago=60)
    first_score = fake_redis.zscore("velocity:CUST_1", "TXN_RETRY")

    first = velocity.record_and_check_velocity("CUST_1", "TXN_RETRY")
    second = velocity.record_and_check_velocity("CUST_1", "TXN_RETRY")

    assert first.tx_count == 1
    assert second.tx_count == 1
    assert fake_redis.zscore("velocity:CUST_1", "TXN_RETRY") > first_score


def test_velocity_customer_isolation(velocity, fake_redis):
    """One customer's burst doesn't leak into another customer's window."""
    for i in range(10):
        result_a = velocity.record_and_check_velocity("CUST_A", f"TXN_A_{i}")
    result_b = velocity.record_and_check_velocity("CUST_B", "TXN_B_0")

    assert result_a.tx_count == 10
    assert result_a.is_velocity_anomaly is True
    assert result_b.tx_count == 1
    assert result_b.is_velocity_anomaly is False
    assert fake_redis.zcard("velocity:CUST_B") == 1


def _unreachable_redis():
    client = MagicMock()
    client.pipeline.return_value.execute.side_effect = redis.ConnectionError("down")
    return client


@pytest.mark.parametrize(
    "redis_client",
    [None, _unreachable_redis()],
    ids=["redis_none", "redis_connection_error"],
)
def test_velocity_redis_unavailable(redis_client):
    """Missing or unreachable Redis fails open with a neutral result."""
    velocity = VelocityWindow(redis_client, window_seconds=WINDOW_SECONDS, max_tx_threshold=MAX_TX_THRESHOLD)

    result = velocity.record_and_check_velocity("CUST_1", "TXN_1")

    assert result.tx_count == 1
    assert result.is_velocity_anomaly is False
    assert result.velocity_risk_points == 0


def test_score_endpoint_flags_velocity_burst(client, auth_headers):
    """The Nth transaction inside the window adds the velocity penalty to the score."""
    threshold = settings.VELOCITY_MAX_THRESHOLD
    base_payload = {
        "customer_id": "CUST_BURST",
        "recipient": "1234567890",
        "provider": "058",
        "amount": 500000,
        "transaction_type": "transfer",
        "medium": "app",
    }

    responses = []
    for i in range(threshold):
        payload = {
            **base_payload,
            "transaction_reference": f"TXN_BURST_{i}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        res = client.post("/v1/score", json=payload, headers=auth_headers)
        assert res.status_code == 200
        responses.append(res.json())

    assert "high_velocity_burst" not in responses[-2]["reasons"]
    assert "high_velocity_burst" in responses[-1]["reasons"]
    assert responses[-1]["score"] - responses[-2]["score"] == settings.VELOCITY_SCORE_PENALTY
