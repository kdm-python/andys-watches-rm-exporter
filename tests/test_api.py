from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

from rm_exporter import api, db
from rm_exporter.models import MedusaAddress, MedusaOrder


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "orders.db")
    monkeypatch.setenv("INTEGRATION_API_KEY", "test-secret")
    monkeypatch.setenv("MEDUSA_BASE_URL", "https://medusa.example")
    monkeypatch.setenv("MEDUSA_ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("ROYAL_MAIL_BASE_URL", "https://royal-mail.example/api/v1")
    monkeypatch.setenv("ROYAL_MAIL_API_KEY", "royal-mail-key")
    with TestClient(api.app) as test_client:
        yield test_client


@pytest.fixture
def medusa_order() -> MedusaOrder:
    return MedusaOrder(
        id="medusa_1",
        display_id=1001,
        created_at="2026-10-06T12:00:00Z",
        email="customer@example.com",
        subtotal=26.79,
        shipping_total=5.00,
        total=31.79,
        shipping_address=MedusaAddress(
            first_name="Ada",
            last_name="Lovelace",
            company=None,
            address_1="1 Example Street",
            address_2=None,
            city="London",
            province=None,
            postal_code="SW1A 1AA",
            country_code="gb",
            phone=None,
        ),
        fulfillment_status="not_fulfilled",
        items=[],
    )


def test_healthcheck(client: TestClient):
    assert client.get("/").json() == {"status": "ok"}


def test_orders_requires_valid_integration_key(client: TestClient):
    response = client.post("/orders", json={"order_id": "medusa_1"})
    assert response.status_code == 422

    response = client.post(
        "/orders",
        headers={"X-Integration-Key": "wrong-secret"},
        json={"order_id": "medusa_1"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Unauthorized"}


def test_orders_transforms_and_records_new_order(
    client: TestClient, medusa_order: MedusaOrder, monkeypatch: pytest.MonkeyPatch
):
    royal_mail_order = Mock(order_reference="AW-1001")
    monkeypatch.setattr(api, "get_order", Mock(return_value=medusa_order))
    monkeypatch.setattr(api, "transform_order", Mock(return_value=royal_mail_order))
    monkeypatch.setattr(api, "to_payload", Mock(return_value={"items": []}))
    send_order = Mock(return_value={"orderIdentifier": "rm_1"})
    monkeypatch.setattr(api, "send_order_click_and_drop", send_order)

    response = client.post(
        "/orders",
        headers={"X-Integration-Key": "test-secret"},
        json={"order_id": "medusa_1"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "order_id": "medusa_1",
        "status": "sent",
        "royal_mail_payload": {"items": []},
        "posted_to_royal_mail": True,
        "royal_mail_response": {"orderIdentifier": "rm_1"},
    }
    stored_order = db.select_order("medusa_1")
    assert stored_order["royal_mail_reference"] == "AW-1001"
    assert stored_order["status"] == "sent"
    send_order.assert_called_once_with(
        "https://royal-mail.example/api/v1", "royal-mail-key", royal_mail_order
    )


def test_orders_retries_a_pending_order(
    client: TestClient, medusa_order: MedusaOrder, monkeypatch: pytest.MonkeyPatch
):
    db.create_order("medusa_1", "AW-1001")
    royal_mail_order = Mock(order_reference="AW-1001")
    monkeypatch.setattr(api, "get_order", Mock(return_value=medusa_order))
    monkeypatch.setattr(api, "transform_order", Mock(return_value=royal_mail_order))
    monkeypatch.setattr(api, "to_payload", Mock(return_value={"items": []}))
    send_order = Mock(return_value={"orderIdentifier": "rm_1"})
    monkeypatch.setattr(api, "send_order_click_and_drop", send_order)

    response = client.post(
        "/orders",
        headers={"X-Integration-Key": "test-secret"},
        json={"order_id": "medusa_1"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert db.select_order("medusa_1")["status"] == "sent"
    send_order.assert_called_once()


def test_orders_does_not_resend_a_sent_order(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    db.create_order("medusa_1", "AW-1001")
    db.update_order_status("medusa_1", "sent")
    send_order = Mock()
    monkeypatch.setattr(api, "send_order_click_and_drop", send_order)

    response = client.post(
        "/orders",
        headers={"X-Integration-Key": "test-secret"},
        json={"order_id": "medusa_1"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "sent",
        "order_id": "medusa_1",
        "already_exists": True,
    }
    send_order.assert_not_called()


def test_orders_marks_a_failed_submission_for_retry(
    client: TestClient, medusa_order: MedusaOrder, monkeypatch: pytest.MonkeyPatch
):
    royal_mail_order = Mock(order_reference="AW-1001")
    monkeypatch.setattr(api, "get_order", Mock(return_value=medusa_order))
    monkeypatch.setattr(api, "transform_order", Mock(return_value=royal_mail_order))
    monkeypatch.setattr(api, "to_payload", Mock(return_value={"items": []}))
    monkeypatch.setattr(
        api,
        "send_order_click_and_drop",
        Mock(side_effect=requests.ConnectionError("connection refused")),
    )

    response = client.post(
        "/orders",
        headers={"X-Integration-Key": "test-secret"},
        json={"order_id": "medusa_1"},
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Royal Mail submission failed"}
    stored_order = db.select_order("medusa_1")
    assert stored_order["status"] == "failed"
    assert stored_order["error"] == "connection refused"
