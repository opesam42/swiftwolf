from datetime import datetime, timezone


def test_settle_transaction_success(client, auth_headers, seed_customer):
    """Verifies successful settlement and async background task scheduling."""
    seed_customer("CUST_100")

    payload = {
        "transaction_reference": "TXN_SETTLE_001",
        "customer_id": "CUST_100",
        "amount": 5000.0,
        "beneficiary_account": "1234567890",
        "beneficiary_bank_code": "058",
        "transaction_type": "transfer",
        "medium": "app",
        "settled_at": datetime.now(timezone.utc).isoformat(),
        "status": "SUCCESS",
    }

    response = client.post("/v1/transactions/settle", json=payload, headers=auth_headers)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "SETTLED"
    assert data["is_settled"] is True


def test_settle_transaction_idempotency_guard(client, auth_headers, seed_customer):
    """Guarantees that re-settling an already settled transaction prevents duplicate ML updates."""
    seed_customer("CUST_100")

    payload = {
        "transaction_reference": "TXN_SETTLE_DUP",
        "customer_id": "CUST_100",
        "amount": 5000.0,
        "beneficiary_account": "1234567890",
        "beneficiary_bank_code": "058",
        "transaction_type": "transfer",
        "medium": "app",
        "settled_at": datetime.now(timezone.utc).isoformat(),
        "status": "SUCCESS",
    }

    # Initial Settlement
    res1 = client.post("/v1/transactions/settle", json=payload, headers=auth_headers)
    assert res1.json()["status"] == "SETTLED"

    # Retry Settlement
    res2 = client.post("/v1/transactions/settle", json=payload, headers=auth_headers)
    assert res2.status_code == 200
    assert res2.json()["status"] == "ALREADY_SETTLED"
    assert "Background ML updates skipped" in res2.json()["message"]