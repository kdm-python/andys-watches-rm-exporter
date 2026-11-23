from copy import deepcopy
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

from rm_exporter import api, db
from rm_exporter.poller import ExporterDispatcher, run_cycle
from rm_exporter.poll_store import PollerBusy, QueueConflict, init_polling_db, polling_store
from rm_exporter.royal_mail_polling import PollHTTPError
from tests.test_polling import rm_order


@pytest.fixture
def polling_db(temporary_database):
    db.init_db()
    init_polling_db()
    init_polling_db()


def export(order_id="order_123", reference="AW-1001"):
    db.create_order(order_id, reference)
    db.update_order_status(order_id, "sent")


def due_now(store):
    store.write("UPDATE royal_mail_polling SET next_check_at = CURRENT_TIMESTAMP")
    store.write("UPDATE royal_mail_poll_inbox SET next_attempt_at = CURRENT_TIMESTAMP")


def test_inbox_persistence_replay_conflicts_and_retry(polling_db):
    payload = {"order_id": "order_123", "status": "despatched"}
    with polling_store() as store:
        event = store.enqueue(payload)
        assert event["status"] == "pending"
        assert store.enqueue(payload)["event_key"] == "order_123"
        with pytest.raises(QueueConflict):
            store.enqueue({**payload, "labels": [{"tracking_number": "OTHER"}]})
        store.attempted("order_123")
        store.event_result("order_123", "review", error="Inspect Medusa")
        assert store.due_events() == []
    with polling_store() as store:
        store.retry_event("order_123")
        assert store.due_events()[0]["attempts"] == 1
        store.event_result("order_123", "delivered", result={"status": "shipped"})
        with pytest.raises(QueueConflict): store.retry_event("order_123")
        assert store.due_events() == []


def test_worker_lock_is_exclusive_but_accepting_manual_alert_is_independent(polling_db):
    with polling_store(worker=True):
        with pytest.raises(PollerBusy):
            with polling_store(worker=True): pass
        with polling_store() as store:
            store.enqueue({"order_id": "order_123", "status": "despatched"})
    with polling_store(worker=True) as store:
        assert len(store.due_events()) == 1


def test_baseline_then_label_then_dispatch_and_restart(polling_db):
    export()
    reader = Mock()
    dispatcher = Mock()
    reader.orders.return_value = [rm_order()]
    with polling_store(worker=True) as store:
        assert run_cycle(store, reader=reader)["observed"] == 1
    # A separate connection simulates the next process start.
    with polling_store(worker=True) as store:
        due_now(store)
        reader.orders.return_value = [rm_order(printedOn="2026-11-23T11:00:00Z")]
        assert run_cycle(store, reader=reader, dispatcher=dispatcher, apply=True, trigger="shipped-on")["queued"] == 0
        due_now(store)
        reader.orders.return_value = [rm_order(printedOn="2026-11-23T11:00:00Z", shippedOn="2026-11-23T12:00:00Z", trackingNumber="TRACK")]
        dispatcher.send.return_value = {"order_id": "order_123", "status": "shipped", "fulfillment_id": "ful_1"}
        result = run_cycle(store, reader=reader, dispatcher=dispatcher, apply=True, trigger="shipped-on")
        assert result["queued"] == result["delivered"] == 1
        assert store.rows("SELECT status FROM royal_mail_polling")[0]["status"] == "synced"
        due_now(store)
        assert run_cycle(store, reader=reader, dispatcher=dispatcher, apply=True, trigger="shipped-on")["queued"] == 0
        assert dispatcher.send.call_count == 1
    assert db.select_order("order_123")["status"] == "sent"


def test_observation_mode_does_not_automatically_send_historical_shipments(polling_db):
    export()
    reader = Mock()
    reader.orders.return_value = [rm_order(shippedOn="2026-11-23T12:00:00Z")]
    with polling_store(worker=True) as store:
        result = run_cycle(store, reader=reader, apply=True, trigger="shipped-on")
        assert result["review"] == 1
        assert store.due_events() == []
        assert store.rows("SELECT status FROM royal_mail_polling")[0]["status"] == "historical"


def test_retry_backoff_then_delivery_and_review(polling_db):
    dispatcher = Mock()
    with polling_store(worker=True) as store:
        store.enqueue({"order_id": "order_123", "status": "despatched"})
        dispatcher.send.side_effect = PollHTTPError("Temporary connection failure")
        assert run_cycle(store, dispatcher=dispatcher, apply=True)["retry"] == 1
        assert store.due_events() == []
        due_now(store)
        dispatcher.send.side_effect = PollHTTPError("Medusa needs review", retryable=False)
        assert run_cycle(store, dispatcher=dispatcher, apply=True)["review"] == 1
        due_now(store)
        assert store.due_events() == []
        store.retry_event("order_123")
        dispatcher.send.side_effect = None
        dispatcher.send.return_value = {"order_id": "order_123", "status": "shipped", "fulfillment_id": "ful_1", "already_shipped": True}
        assert run_cycle(store, dispatcher=dispatcher, apply=True)["delivered"] == 1


def test_order_filter_limits_reads_and_sends(polling_db):
    export()
    export("order_456", "AW-1002")
    reader = Mock()
    reader.orders.return_value = [rm_order()]
    with polling_store(worker=True) as store:
        store.enqueue({"order_id": "order_123", "status": "despatched"})
        store.enqueue({"order_id": "order_456", "status": "despatched"})
        result = run_cycle(store, reader=reader, order_id="order_123")
        assert result["observed"] == result["would_dispatch"] == 1
        reader.orders.assert_called_once_with(["AW-1001"])


def test_delivered_inbox_is_reconciled_if_watch_inserted_later(polling_db):
    with polling_store() as store:
        store.enqueue({"order_id": "order_123", "status": "despatched"})
        store.event_result("order_123", "delivered", result={"status": "shipped"})
        export()
        store.discover()
        assert store.due_orders() == []


def test_api_inbox_worker_existing_dispatch_endpoint_end_to_end(polling_db, monkeypatch):
    # Real durable queue + real request validation/dispatch handler. Medusa alone
    # is simulated: no customer email or carrier call is made by this test.
    monkeypatch.setenv("INTEGRATION_API_KEY", "secret")
    monkeypatch.delenv("DISPATCH_API_KEY", raising=False)
    monkeypatch.setenv("MEDUSA_BASE_URL", "http://medusa.example")
    monkeypatch.setenv("MEDUSA_ADMIN_API_KEY", "secret")
    order = {"id": "order_123", "status": "pending", "payment_status": "captured",
             "items": [{"id": "ordli_1", "quantity": 1, "requires_shipping": False,
                        "detail": {"fulfilled_quantity": 1, "shipped_quantity": 0}}],
             "fulfillments": [{"id": "ful_1", "provider_id": "manual_manual", "shipping_option_id": "so_1",
                               "items": [{"line_item_id": "ordli_1", "quantity": 1}]}]}
    medusa = Mock()
    medusa.order.side_effect = lambda _: deepcopy(order)
    medusa.shipping_option.return_value = {"id": "so_1", "provider_id": "manual_manual"}
    def ship(*_): order["fulfillments"][0]["shipped_at"] = "2026-11-23T12:00:00Z"
    medusa.ship.side_effect = ship
    monkeypatch.setattr(api, "MedusaDispatchClient", lambda *_: medusa)
    with TestClient(api.app) as client:
        headers = {"X-Integration-Key": "secret"}
        payload = {"order_id": "order_123", "status": "despatched"}
        assert client.post("/polling/dispatch", headers=headers, json=payload).status_code == 202
        medusa.order.assert_not_called()
        assert client.post("/polling/dispatch", headers=headers, json=payload).status_code == 202
        # Route the worker's actual HTTP client into the real FastAPI application.
        monkeypatch.setattr(requests, "post", lambda url, **kw: client.post(
            "/orders/dispatch", json=kw["json"], headers=kw["headers"]))
        with polling_store(worker=True) as store:
            assert run_cycle(store)["would_dispatch"] == 1
            assert run_cycle(store, dispatcher=ExporterDispatcher("http://exporter", "secret"), apply=True)["delivered"] == 1
            assert run_cycle(store, dispatcher=ExporterDispatcher("http://exporter", "secret"), apply=True)["delivered"] == 0
        status = client.get("/polling/events/order_123", headers=headers).json()
        assert status["status"] == "delivered"
        assert status["attempts"] == 1
        assert status["result"]["fulfillment_id"] == "ful_1"
        medusa.ship.assert_called_once_with("order_123", "ful_1", [{"id": "ordli_1", "quantity": 1}], [])


@pytest.mark.parametrize("path,body,headers,expected", [
    ("/polling/dispatch", {"order_id": "order_123", "status": "despatched"}, {}, 422),
    ("/polling/dispatch", {"order_id": "order_123", "status": "despatched"}, {"X-Integration-Key": "bad"}, 403),
    ("/polling/dispatch", {"order_id": "order_123", "status": "label_generated"}, {"X-Integration-Key": "secret"}, 422),
])
def test_inbox_auth_and_event_validation(polling_db, monkeypatch, path, body, headers, expected):
    monkeypatch.setenv("INTEGRATION_API_KEY", "secret")
    monkeypatch.delenv("DISPATCH_API_KEY", raising=False)
    response = TestClient(api.app).post(path, headers=headers, json=body)
    assert response.status_code == expected
    with polling_store() as store: assert store.due_events() == []
