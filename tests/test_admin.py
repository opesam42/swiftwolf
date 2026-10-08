from datetime import datetime, timezone

from src.core.config import settings
from src.scoring.models import Decision


def _login(client, username=None, password=None, customer_id="CUST_DASH"):
    return client.post(
        "/admin/login",
        data={
            "username": username if username is not None else settings.ADMIN_USERNAME,
            "password": password if password is not None else settings.ADMIN_PASSWORD,
            "customer_id": customer_id,
        },
        follow_redirects=False,
    )


def test_export_beneficiaries_unauthorized(client):
    """Verifies that missing admin headers return 401 Unauthorized."""
    response = client.get("/v1/internal/beneficiaries")
    assert response.status_code == 401


def test_export_beneficiaries_success(client, admin_auth_headers):
    """Verifies authorized admin export of blacklisted accounts."""
    response = client.get("/v1/internal/beneficiaries", headers=admin_auth_headers)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "success"
    assert "count" in data
    assert isinstance(data["data"], list)


def test_admin_page_shows_login_when_anonymous(client):
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Sign in" in response.text
    assert "sw_admin_session" not in response.cookies


def test_admin_login_rejects_wrong_password(client):
    response = _login(client, password="not-the-dashboard-password")
    assert response.status_code == 401
    assert "Invalid username or password" in response.text


def test_admin_login_rejects_api_key_as_password(client):
    """Dashboard password is not SWIFTWOLF_API_KEY."""
    response = _login(client, password=settings.SWIFTWOLF_API_KEY)
    assert response.status_code == 401


def test_admin_login_sets_httponly_cookie_and_lands_on_dashboard(client):
    response = _login(client)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin")
    cookie = response.cookies.get("sw_admin_session")
    assert cookie
    assert "httponly" in response.headers.get("set-cookie", "").lower()

    dashboard = client.get("/admin?customer_id=CUST_DASH")
    assert dashboard.status_code == 200
    assert "Recent-habit control band" in dashboard.text
    assert "CUST_DASH" in dashboard.text


def test_control_chart_requires_session_cookie(client):
    response = client.get("/admin/api/customers/CUST_DASH/control-chart")
    assert response.status_code == 401


def test_control_chart_404_for_unknown_customer(client):
    _login(client)
    response = client.get("/admin/api/customers/nobody_here/control-chart")
    assert response.status_code == 404


def test_control_chart_returns_ewma_band_and_scored_points(
    client, auth_headers, seed_customer
):
    seed_customer(
        "CUST_DASH",
        category_baselines={
            "transfer": {
                "count": 40,
                "avg_amount": 739.33,
                "m2": 0,
                "std_amount": 200.0,
                "ewma_avg": 739.33,
                "ewma_var": 987868.0,
                "ewma_std": 993.92,
            }
        },
        typing_baselines={
            "sample_count": 30,
            "fields": {
                "flight_time_ms": [
                    {
                        "id": i + 1,
                        "ewma_avg": 80.0 + i,
                        "ewma_var": 25.0,
                        "ewma_std": 5.0,
                        "sample_count": 10,
                    }
                    for i in range(3)
                ]
            },
        },
    )
    score = client.post(
        "/v1/score",
        json={
            "transaction_reference": "TXN_DASH_SPIKE",
            "customer_id": "CUST_DASH",
            "recipient": "0999888777",
            "provider": "058",
            "amount": 1000000,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "transaction_type": "transfer",
            "medium": "app",
        },
        headers=auth_headers,
    )
    assert score.status_code == 200
    assert score.json()["decision"] == Decision.STEP_UP.value

    _login(client)
    response = client.get("/admin/api/customers/CUST_DASH/control-chart")
    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == "CUST_DASH"
    assert body["ewma_avg"] == 739.33
    assert body["ewma_std"] == 993.92
    assert body["k_active"] == 3
    assert body["k_max"] == 5
    assert body["category"] == "transfer"
    assert body["band_z"] == 2.5
    assert body["upper"] == 3224.13
    assert body["lower"] == 0.0
    assert [zone["z"] for zone in body["zones"]] == [1.0, 2.5, 3.0, 4.0, 5.0]
    assert body["zones"][0]["upper"] == 1733.25
    assert body["zones"][-1]["upper"] == 5708.93
    assert body["latency_ms"] >= 1
    assert len(body["points"]) == 1
    point = body["points"][0]
    assert point["amount"] == 10000.0
    assert point["decision"] == Decision.STEP_UP.value
    assert point["pending"] is True
    assert point["z"] > 2.5


def test_control_chart_includes_seeded_transfers_without_risk_events(
    client, db_session, seed_customer
):
    from src.settlement.models import Transaction

    seed_customer(
        "CUST_SEED_DOTS",
        category_baselines={
            "transfer": {
                "count": 10,
                "avg_amount": 500.0,
                "m2": 0,
                "std_amount": 50.0,
                "ewma_avg": 500.0,
                "ewma_var": 2500.0,
                "ewma_std": 50.0,
            }
        },
    )
    db_session.add(
        Transaction(
            transaction_reference="TXN_STMT_1",
            customer_id="CUST_SEED_DOTS",
            amount=50000,
            destination_key="transfer:058:111",
            provider="058",
            transaction_type="transfer",
            medium="statement",
            is_settled=True,
            occurred_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    _login(client, customer_id="CUST_SEED_DOTS")
    response = client.get("/admin/api/customers/CUST_SEED_DOTS/control-chart")
    assert response.status_code == 200
    body = response.json()
    assert len(body["points"]) == 1
    assert body["points"][0]["amount"] == 500.0
    assert body["points"][0]["decision"] is None
    assert body["points"][0]["pending"] is False


def test_control_chart_rejects_unknown_category(client, seed_customer):
    seed_customer("CUST_CAT")
    _login(client)
    response = client.get("/admin/api/customers/CUST_CAT/control-chart?category=crypto")
    assert response.status_code == 400


def test_control_chart_uses_selected_category_baseline(client, db_session, seed_customer):
    from src.settlement.models import Transaction

    seed_customer(
        "CUST_CATS",
        category_baselines={
            "transfer": {
                "count": 10,
                "avg_amount": 739.33,
                "m2": 0,
                "std_amount": 100.0,
                "ewma_avg": 739.33,
                "ewma_var": 10000.0,
                "ewma_std": 993.92,
            },
            "airtime": {
                "count": 8,
                "avg_amount": 200.0,
                "m2": 0,
                "std_amount": 20.0,
                "ewma_avg": 200.0,
                "ewma_var": 400.0,
                "ewma_std": 20.0,
            },
        },
    )
    db_session.add(
        Transaction(
            transaction_reference="TXN_AIR_1",
            customer_id="CUST_CATS",
            amount=20000,
            destination_key="airtime:MTN:0803",
            provider="MTN",
            transaction_type="airtime",
            medium="statement",
            is_settled=True,
            occurred_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    _login(client)
    response = client.get("/admin/api/customers/CUST_CATS/control-chart?category=airtime")
    assert response.status_code == 200
    body = response.json()
    assert body["category"] == "airtime"
    assert body["ewma_avg"] == 200.0
    assert body["ewma_std"] == 20.0
    assert body["zones"][0]["upper"] == 220.0
    assert body["zones"][-1]["upper"] == 300.0
    assert len(body["points"]) == 1
    assert body["points"][0]["amount"] == 200.0
    assert body["points"][0]["pending"] is False


def test_control_chart_keeps_unsettled_statement_off_history_and_live_score_on_the_right(
    client, auth_headers, db_session, seed_customer
):
    from src.settlement.models import Transaction

    seed_customer(
        "CUST_SPLIT",
        category_baselines={
            "transfer": {
                "count": 40,
                "avg_amount": 739.33,
                "m2": 0,
                "std_amount": 200.0,
                "ewma_avg": 739.33,
                "ewma_var": 987868.0,
                "ewma_std": 993.92,
            }
        },
    )
    db_session.add(
        Transaction(
            transaction_reference="TXN_UNSETTLED_STMT",
            customer_id="CUST_SPLIT",
            amount=50000,
            destination_key="transfer:058:111",
            provider="058",
            transaction_type="transfer",
            medium="statement",
            is_settled=False,
            occurred_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    score = client.post(
        "/v1/score",
        json={
            "transaction_reference": "TXN_LIVE_PENDING",
            "customer_id": "CUST_SPLIT",
            "recipient": "0123456789",
            "provider": "058",
            "amount": 1000000,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "transaction_type": "transfer",
            "medium": "app",
        },
        headers=auth_headers,
    )
    assert score.status_code == 200

    _login(client)
    body = client.get("/admin/api/customers/CUST_SPLIT/control-chart").json()
    assert [point["amount"] for point in body["points"] if not point["pending"]] == []
    live = [point for point in body["points"] if point["pending"]]
    assert len(live) == 1
    assert live[0]["amount"] == 10000.0
    assert live[0]["decision"] == Decision.STEP_UP.value
