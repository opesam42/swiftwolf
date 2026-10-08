"""API-level guarantees the integration guide promises to bank developers."""
from datetime import datetime, timezone

import pytest

from src.blacklist.repository import BlacklistRepository


def test_base_url_is_a_health_ping(client):
    """GET / answers without auth, so load balancers and integrators can check the service is up."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "SwiftWolf"}


def test_app_startup_uses_the_test_redis_not_the_real_one(client, fake_redis):
    """Startup warms the blacklist cache; in tests that must land in fakeredis, never the REDIS_URL in .env."""
    assert fake_redis.exists(BlacklistRepository.MARKER_KEY)


def _score_payload(**overrides) -> dict:
    return {
        "transaction_reference": "TXN_LEN",
        "customer_id": "CUST_LEN",
        "provider": "058",
        "recipient": "0123456789",
        "amount": 500000,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transaction_type": "transfer",
        "medium": "app",
        **overrides,
    }


@pytest.mark.parametrize(
    "field, limit",
    [("transaction_reference", 64), ("customer_id", 64), ("provider", 30), ("recipient", 50)],
)
def test_score_rejects_over_long_fields_with_422(client, auth_headers, field, limit):
    """Values longer than their stored column are a clean 422, not a database error (500)."""
    at_limit = client.post("/v1/score", json=_score_payload(**{field: "9" * limit}), headers=auth_headers)
    assert at_limit.status_code == 200

    # a fresh reference so the first call's stored decision isn't replayed (when field is the reference, it's replaced anyway)
    too_long = client.post(
        "/v1/score",
        json=_score_payload(**{"transaction_reference": "TXN_LEN_2", field: "9" * (limit + 1)}),
        headers=auth_headers,
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"][0]["loc"] == ["body", field]


def test_score_rejects_statement_medium_with_422(client, auth_headers):
    """statement is seed-job history, not a live bank channel."""
    response = client.post(
        "/v1/score",
        json=_score_payload(medium="statement"),
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "medium"]


def test_settle_rejects_over_long_reference_with_422(client, auth_headers):
    response = client.post(
        "/v1/transactions/settle",
        json={"transaction_reference": "X" * 65, "settled_at": datetime.now(timezone.utc).isoformat()},
        headers=auth_headers,
    )
    assert response.status_code == 422
