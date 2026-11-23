from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

from rm_exporter import api
from rm_exporter.dispatch import (
    DispatchError, MedusaDispatchClient, MedusaError, dispatch_order,
)
from rm_exporter.dispatch_store import DispatchBusy


class MemoryStore:
    def __init__(self):
        self.rows = {}

    def get(self, key):
        return deepcopy(self.rows.get(key))

    def create(self, key, order_id, labels):
        self.rows[key] = {"dispatch_key": key, "medusa_order_id": order_id,
                          "status": "pending", "labels": labels, "fulfillment_id": None}
        return self.get(key)

    def update(self, key, status, fulfillment_id=None):
        self.rows[key]["status"] = status
        if fulfillment_id:
            self.rows[key]["fulfillment_id"] = fulfillment_id

    def has_other_unresolved(self, order_id, key):
        return any(row["medusa_order_id"] == order_id and other_key != key
                   and row["status"] in ("fulfillment_requested", "shipment_requested")
                   for other_key, row in self.rows.items())


@pytest.fixture
def scenario():
    order = {"id": "order_123", "status": "pending", "payment_status": "captured",
             "items": [{"id": "ordli_1", "quantity": 2, "requires_shipping": False,
                        "detail": {"fulfilled_quantity": 0, "shipped_quantity": 0}}],
             "shipping_methods": [{"shipping_option_id": "so_1"}], "fulfillments": []}
    option = {"id": "so_1", "provider_id": "manual_manual", "type": {"code": "express"},
              "service_zone": {"fulfillment_set": {"location": {"id": "sloc_1"}}}}
    fulfillment = {"id": "ful_1", "provider_id": "manual_manual", "shipping_option_id": "so_1",
                   "items": [{"line_item_id": "ordli_1", "quantity": 2}], "labels": [],
                   "shipped_at": None, "canceled_at": None}
    client = Mock()
    client.order.side_effect = lambda _: deepcopy(order)
    client.shipping_option.return_value = option

    def create(*_):
        order["fulfillments"].append(deepcopy(fulfillment))
        order["items"][0]["detail"]["fulfilled_quantity"] = 2

    def ship(_order_id, fid, _items, labels):
        target = next(f for f in order["fulfillments"] if f["id"] == fid)
        target["shipped_at"] = "2026-11-23T12:00:00Z"
        target["labels"] = labels

    client.create_fulfillment.side_effect = create
    client.ship.side_effect = ship
    return client, MemoryStore(), order, option, fulfillment


def test_order_only_dispatch_creates_and_ships_once(scenario):
    client, store, order, option, _ = scenario
    result = dispatch_order(client, store, "order_123")
    assert result == {"order_id": "order_123", "fulfillment_id": "ful_1", "status": "shipped", "already_shipped": False}
    client.create_fulfillment.assert_called_once_with("order_123", [{"id": "ordli_1", "quantity": 2}], option)
    client.ship.assert_called_once_with("order_123", "ful_1", [{"id": "ordli_1", "quantity": 2}], [])
    assert store.get("order_123")["status"] == "shipped"
    assert dispatch_order(client, store, "order_123")["already_shipped"]
    assert client.create_fulfillment.call_count == client.ship.call_count == 1


def test_existing_fulfillment_is_reused_and_tracking_is_preserved(scenario):
    client, store, order, _, fulfillment = scenario
    fulfillment["labels"] = [{"tracking_number": "TRACK", "tracking_url": "", "label_url": ""}]
    order["fulfillments"] = [fulfillment]
    dispatch_order(client, store, "order_123")
    client.create_fulfillment.assert_not_called()
    assert client.ship.call_args.args[3] == fulfillment["labels"]


def test_tracking_supplied_and_conflicting_replay_rejected(scenario):
    client, store, *_ = scenario
    labels = [{"tracking_number": "TRACK", "tracking_url": "https://example.com/track", "label_url": ""}]
    dispatch_order(client, store, "order_123", labels=labels)
    assert client.ship.call_args.args[3] == labels
    with pytest.raises(DispatchError, match="differ"):
        dispatch_order(client, store, "order_123", labels=[{**labels[0], "tracking_number": "DIFFERENT"}])


@pytest.mark.parametrize("problem", ["unpaid", "canceled", "no items", "partial quantities", "missing shipping", "multiple shipping", "mixed items", "carrier", "pickup", "no location", "returns"])
def test_rejects_unsupported_orders_before_any_write(scenario, problem):
    client, store, order, option, _ = scenario
    if problem == "unpaid": order["payment_status"] = "authorized"
    if problem == "canceled": order["status"] = "canceled"
    if problem == "no items": order["items"] = []
    if problem == "partial quantities": order["items"][0]["detail"]["fulfilled_quantity"] = 1
    if problem == "missing shipping": order["shipping_methods"] = []
    if problem == "multiple shipping": order["shipping_methods"] *= 2
    if problem == "mixed items": order["items"].append({**order["items"][0], "id": "ordli_2", "requires_shipping": True})
    if problem == "carrier": option["provider_id"] = "carrier_booking"
    if problem == "pickup": option["type"]["code"] = "pickup"
    if problem == "no location": option["service_zone"] = None
    if problem == "returns": order["items"][0]["detail"]["return_requested_quantity"] = 1
    with pytest.raises(DispatchError): dispatch_order(client, store, "order_123")
    client.create_fulfillment.assert_not_called()
    client.ship.assert_not_called()


def test_partial_fulfillment_requires_explicit_selection(scenario):
    client, store, order, _, fulfillment = scenario
    fulfillment["items"][0]["quantity"] = 1
    order["fulfillments"] = [fulfillment]
    with pytest.raises(DispatchError, match="only part"):
        dispatch_order(client, store, "order_123")
    dispatch_order(client, store, "order_123", fulfillment_id="ful_1")
    assert client.ship.call_args.args[2] == [{"id": "ordli_1", "quantity": 1}]


def test_multiple_fulfillments_require_selection(scenario):
    client, store, order, _, fulfillment = scenario
    order["fulfillments"] = [fulfillment, {**fulfillment, "id": "ful_2"}]
    with pytest.raises(DispatchError, match="multiple fulfillments"):
        dispatch_order(client, store, "order_123")
    dispatch_order(client, store, "order_123", fulfillment_id="ful_2")
    assert client.ship.call_args.args[1] == "ful_2"


def test_cancelled_or_foreign_fulfillment_is_not_replaced(scenario):
    client, store, order, _, fulfillment = scenario
    fulfillment["canceled_at"] = "2026-11-23"
    order["fulfillments"] = [fulfillment]
    with pytest.raises(DispatchError, match="missing, canceled"):
        dispatch_order(client, store, "order_123", fulfillment_id="ful_1")
    client.create_fulfillment.assert_not_called()


def test_inventory_kits_use_purchased_quantities(scenario):
    client, store, order, _, fulfillment = scenario
    order["items"][0]["variant"] = {"inventory_items": [{"inventory": {"id": "inv_1"}, "required_quantity": 3}]}
    fulfillment["items"] = [{"line_item_id": "ordli_1", "inventory_item_id": "inv_1", "quantity": 6}]
    order["fulfillments"] = [fulfillment]
    dispatch_order(client, store, "order_123")
    assert client.ship.call_args.args[2] == [{"id": "ordli_1", "quantity": 2}]


@pytest.mark.parametrize("committed", [True, False])
def test_fulfillment_timeout_is_reconciled_without_second_creation(scenario, committed):
    client, store, *_ = scenario
    create = client.create_fulfillment.side_effect

    def timeout(*args):
        if committed: create(*args)
        raise MedusaError(504, "Timed out")

    client.create_fulfillment.side_effect = timeout
    with pytest.raises(MedusaError): dispatch_order(client, store, "order_123")
    if committed:
        assert not dispatch_order(client, store, "order_123")["already_shipped"]
    else:
        with pytest.raises(DispatchError, match="unresolved"):
            dispatch_order(client, store, "order_123")
    assert client.create_fulfillment.call_count == 1


@pytest.mark.parametrize("committed", [True, False])
def test_shipment_timeout_is_reconciled_without_second_shipment(scenario, committed):
    client, store, *_ = scenario
    ship = client.ship.side_effect

    def timeout(*args):
        if committed: ship(*args)
        raise MedusaError(504, "Timed out")

    client.ship.side_effect = timeout
    with pytest.raises(MedusaError): dispatch_order(client, store, "order_123")
    if committed:
        assert dispatch_order(client, store, "order_123")["already_shipped"]
    else:
        with pytest.raises(DispatchError, match="unresolved"):
            dispatch_order(client, store, "order_123")
        with pytest.raises(DispatchError, match="Another dispatch"):
            dispatch_order(client, store, "order_123", fulfillment_id="ful_1")
    assert client.ship.call_count == 1


def test_known_validation_failure_allows_a_corrected_retry(scenario):
    client, store, *_ = scenario
    create = client.create_fulfillment.side_effect
    client.create_fulfillment.side_effect = MedusaError(409, "No reservation", 400)
    with pytest.raises(MedusaError): dispatch_order(client, store, "order_123")
    assert store.get("order_123")["status"] == "pending"
    client.create_fulfillment.side_effect = create
    dispatch_order(client, store, "order_123")
    client.ship.assert_called_once()


def test_native_admin_request_contract(monkeypatch):
    request = Mock(return_value=Mock(status_code=200, json=lambda: {"order": {"id": "order_123"}}))
    monkeypatch.setattr(requests, "request", request)
    client = MedusaDispatchClient("http://localhost:9000/", "secret")
    client.order("order_123")
    assert request.call_args.args == ("GET", "http://localhost:9000/admin/orders/order_123")
    assert request.call_args.kwargs["headers"] == {"Authorization": "Basic secret"}
    client.create_fulfillment("order_123", [{"id": "ordli_1", "quantity": 1}], {
        "id": "so_1", "service_zone": {"fulfillment_set": {"location": {"id": "sloc_1"}}}})
    assert request.call_args.kwargs["json"]["no_notification"] is True
    client.ship("order_123", "ful_1", [{"id": "ordli_1", "quantity": 1}], [])
    assert request.call_args.args == ("POST", "http://localhost:9000/admin/orders/order_123/fulfillments/ful_1/shipments")
    assert request.call_args.kwargs["json"]["no_notification"] is False
    assert request.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("upstream,expected", [(400, 409), (401, 502), (403, 502), (404, 404), (500, 502)])
def test_medusa_http_errors_are_explicit(monkeypatch, upstream, expected):
    monkeypatch.setattr(requests, "request", Mock(return_value=Mock(status_code=upstream, json=lambda: {"message": "Rejected"})))
    with pytest.raises(MedusaError) as caught:
        MedusaDispatchClient("http://localhost:9000", "secret").order("order_123")
    assert caught.value.status_code == expected


@pytest.fixture
def dispatch_api(monkeypatch, scenario):
    medusa, store, *_ = scenario
    monkeypatch.setenv("INTEGRATION_API_KEY", "test-secret")
    monkeypatch.delenv("DISPATCH_API_KEY", raising=False)
    monkeypatch.setenv("MEDUSA_BASE_URL", "http://medusa.example")
    monkeypatch.setenv("MEDUSA_ADMIN_API_KEY", "secret")

    @contextmanager
    def locked(_): yield store

    monkeypatch.setattr(api, "locked_dispatch", locked)
    monkeypatch.setattr(api, "MedusaDispatchClient", lambda *_: medusa)
    # No lifespan: database initialization is tested separately with PostgreSQL.
    return TestClient(api.app), medusa


def test_dispatch_http_endpoint(dispatch_api):
    client, medusa = dispatch_api
    for expected in [False, True]:
        response = client.post("/orders/dispatch", headers={"X-Integration-Key": "test-secret"}, json={"order_id": "order_123", "status": "despatched"})
        assert response.status_code == 200, response.text
        assert response.json()["already_shipped"] is expected
    medusa.ship.assert_called_once()


@pytest.mark.parametrize("headers,body,expected", [
    ({}, {"order_id": "order_123", "status": "despatched"}, 422),
    ({"X-Integration-Key": "wrong"}, {"order_id": "order_123", "status": "despatched"}, 403),
    ({"X-Integration-Key": "test-secret"}, {"order_id": "order_123", "status": "label_generated"}, 422),
    ({"X-Integration-Key": "test-secret"}, {"order_id": "../orders", "status": "despatched"}, 422),
    ({"X-Integration-Key": "test-secret"}, {"order_id": "order_123", "status": "despatched", "labels": [{"tracking_number": "X", "tracking_url": "javascript:alert(1)"}]}, 422),
])
def test_invalid_dispatch_never_contacts_medusa(dispatch_api, headers, body, expected):
    client, medusa = dispatch_api
    assert client.post("/orders/dispatch", headers=headers, json=body).status_code == expected
    medusa.order.assert_not_called()


def test_separate_dispatch_key_supported(dispatch_api, monkeypatch):
    client, medusa = dispatch_api
    monkeypatch.setenv("DISPATCH_API_KEY", "dispatch-secret")
    response = client.post("/orders/dispatch", headers={"X-Integration-Key": "test-secret"}, json={"order_id": "order_123", "status": "despatched"})
    assert response.status_code == 403
    response = client.post("/orders/dispatch", headers={"X-Integration-Key": "dispatch-secret"}, json={"order_id": "order_123", "status": "despatched"})
    assert response.status_code == 200


def test_concurrent_request_returns_conflict(dispatch_api, monkeypatch):
    client, medusa = dispatch_api
    @contextmanager
    def busy(_):
        raise DispatchBusy()
        yield
    monkeypatch.setattr(api, "locked_dispatch", busy)
    response = client.post("/orders/dispatch", headers={"X-Integration-Key": "test-secret"}, json={"order_id": "order_123", "status": "despatched"})
    assert response.status_code == 409
    medusa.order.assert_not_called()


@pytest.mark.parametrize("error,expected", [(requests.Timeout(), 504), (requests.ConnectionError(), 502)])
def test_transport_errors_are_reported(monkeypatch, error, expected):
    monkeypatch.setattr(requests, "request", Mock(side_effect=error))
    with pytest.raises(MedusaError) as caught:
        MedusaDispatchClient("http://localhost:9000", "secret").order("order_123")
    assert caught.value.status_code == expected


@pytest.mark.parametrize("body", [[], {}, {"order": None}])
def test_malformed_order_response_fails_explicitly(monkeypatch, body):
    monkeypatch.setattr(requests, "request", Mock(return_value=Mock(status_code=200, json=lambda: body)))
    with pytest.raises(MedusaError):
        MedusaDispatchClient("http://localhost:9000", "secret").order("order_123")


def test_empty_labels_preserve_tracking_on_replay(scenario):
    client, store, order, _, fulfillment = scenario
    fulfillment["labels"] = [{"tracking_number": "TRACK", "tracking_url": "", "label_url": ""}]
    order["fulfillments"] = [fulfillment]
    dispatch_order(client, store, "order_123", labels=[])
    assert dispatch_order(client, store, "order_123", labels=[])["already_shipped"] is True


def test_medusa_write_error_is_returned_over_http(dispatch_api):
    client, medusa = dispatch_api
    medusa.ship.side_effect = MedusaError(409, "Medusa rejected the shipment", 400)
    response = client.post("/orders/dispatch", headers={"X-Integration-Key": "test-secret"}, json={"order_id": "order_123", "status": "despatched"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Medusa rejected the shipment"


def test_missing_credentials_are_reported_before_medusa_call(dispatch_api, monkeypatch):
    client, medusa = dispatch_api
    monkeypatch.delenv("MEDUSA_ADMIN_API_KEY")
    response = client.post("/orders/dispatch", headers={"X-Integration-Key": "test-secret"}, json={"order_id": "order_123", "status": "despatched"})
    assert response.status_code == 503
    medusa.order.assert_not_called()
