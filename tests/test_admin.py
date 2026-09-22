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