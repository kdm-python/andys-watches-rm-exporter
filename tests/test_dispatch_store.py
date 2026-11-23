from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from rm_exporter import db
from rm_exporter.dispatch_store import DispatchBusy, locked_dispatch


@pytest.fixture
def dispatch_db(temporary_database):
    db.init_db()


def test_dispatch_progress_is_durable_and_export_records_are_unchanged(dispatch_db):
    db.create_order("order_123", "AW-1001")
    with locked_dispatch("order_123") as store:
        record = store.create("order_123", "order_123", [{"tracking_number": "TRACK"}])
        assert record["status"] == "pending"
        store.update("order_123", "fulfillment_requested")
    with locked_dispatch("order_123") as store:
        assert store.get("order_123")["status"] == "fulfillment_requested"
        store.update("order_123", "fulfillment_created", "ful_123")
        assert store.get("order_123")["fulfillment_id"] == "ful_123"
        assert store.get("order_123")["labels"] == [{"tracking_number": "TRACK"}]
        assert store.has_other_unresolved("order_123", "order_123:ful_123") is False
        store.update("order_123", "shipment_requested")
        assert store.has_other_unresolved("order_123", "order_123:ful_123") is True
    assert db.select_order("order_123")["status"] == "pending"


def test_order_lock_excludes_other_workers_and_releases_after_error(dispatch_db):
    with locked_dispatch("order_123"):
        with pytest.raises(DispatchBusy):
            with locked_dispatch("order_123"):
                pytest.fail("Second worker acquired the same order lock")
        with locked_dispatch("order_other"):
            pass
    with pytest.raises(ValueError):
        with locked_dispatch("order_123"):
            raise ValueError("simulated failure")
    with locked_dispatch("order_123"):
        pass


def test_concurrent_http_dispatch_only_sends_one_shipment(dispatch_db, monkeypatch):
    # Real PostgreSQL locking/state with a deterministic fake Medusa API.
    from fastapi.testclient import TestClient
    from rm_exporter import api
    from copy import deepcopy

    entered = Event()
    release = Event()
    order = {"id": "order_123", "status": "pending", "payment_status": "captured",
             "items": [{"id": "ordli_1", "quantity": 1, "requires_shipping": False,
                        "detail": {"fulfilled_quantity": 1, "shipped_quantity": 0}}],
             "fulfillments": [{"id": "ful_123", "provider_id": "manual_manual", "shipping_option_id": "so_1",
                               "items": [{"line_item_id": "ordli_1", "quantity": 1}]}]}
    calls = []

    class Medusa:
        def order(self, _): return deepcopy(order)
        def shipping_option(self, _): return {"id": "so_1", "provider_id": "manual_manual"}
        def ship(self, *_):
            calls.append("ship")
            entered.set()
            assert release.wait(5)
            order["fulfillments"][0]["shipped_at"] = "2026-11-23"

    monkeypatch.setenv("INTEGRATION_API_KEY", "test-secret")
    monkeypatch.delenv("DISPATCH_API_KEY", raising=False)
    monkeypatch.setenv("MEDUSA_BASE_URL", "http://medusa.example")
    monkeypatch.setenv("MEDUSA_ADMIN_API_KEY", "secret")
    monkeypatch.setattr(api, "MedusaDispatchClient", lambda *_: Medusa())
    client = TestClient(api.app)

    def send():
        return client.post("/orders/dispatch", headers={"X-Integration-Key": "test-secret"},
                           json={"order_id": "order_123", "status": "despatched"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send)
        try:
            assert entered.wait(5)
            assert send().status_code == 409
        finally:
            release.set()
        assert first.result().status_code == 200
    assert send().json()["already_shipped"] is True
    assert calls == ["ship"]
    with locked_dispatch("order_123") as store:
        assert store.get("order_123")["status"] == "shipped"
