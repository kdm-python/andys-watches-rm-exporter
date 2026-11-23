from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import requests

from rm_exporter import poller
from rm_exporter.poller import ExporterDispatcher, run_cycle
from rm_exporter.royal_mail_polling import (
    PollHTTPError, RoyalMailReader, classify, dispatch_payload, observation,
)


def rm_order(**changes):
    return {"orderIdentifier": 123, "orderReference": "AW-1001", "printedOn": None,
            "shippedOn": None, "manifestedOn": None, "trackingNumber": None, **changes}


def watch(**changes):
    return {"medusa_order_id": "order_123", "royal_mail_reference": "AW-1001",
            "first_observed_at": None, "baseline_shipped_on": None, "failures": 0,
            "observation": None, **changes}


@pytest.mark.parametrize("raw", [rm_order(), rm_order(printedOn="2026-11-23T12:00:00Z"),
                                 rm_order(manifestedOn="2026-11-23T12:00:00Z")])
def test_labels_and_manifests_never_detect_dispatch(raw):
    current = observation(raw)
    assert classify(watch(), current) == "watching"
    assert classify(watch(first_observed_at=datetime.now(timezone.utc), observation=observation(rm_order())), current) == "watching"


def test_historical_shipments_are_not_automatic_dispatches():
    assert classify(watch(), observation(rm_order(shippedOn="2026-11-23T12:00:00Z"))) == "historical"


def test_dispatch_transition_survives_later_manifesting():
    row = watch(first_observed_at=datetime.now(timezone.utc), observation=observation(rm_order()))
    current = observation(rm_order(shippedOn="2026-11-23T12:00:00Z", manifestedOn="2026-11-23T18:30:00Z"))
    assert classify(row, current) == "detected"
    row["observation"] = current
    assert classify(row, current) == "detected"
    assert classify(row, observation(rm_order())) == "review"


def test_royal_mail_identity_change_needs_review():
    row = watch(first_observed_at=datetime.now(timezone.utc), observation=observation(rm_order()))
    assert classify(row, observation(rm_order(orderIdentifier=456))) == "review"


@pytest.mark.parametrize("value", ["not a date", "2026-11-23", 123])
def test_uncertain_timestamp_never_becomes_dispatch(value):
    with pytest.raises(PollHTTPError): observation(rm_order(shippedOn=value))


def test_default_dotnet_timestamp_is_not_dispatch():
    assert observation(rm_order(shippedOn="0001-01-01T00:00:00"))["shippedOn"] is None


def test_live_personal_account_timestamp_without_offset_is_preserved():
    assert observation(rm_order(printedOn="2026-11-23T12:00:00.123"))["printedOn"] == "2026-11-23T12:00:00.123000"


def test_missing_reference_in_batch_does_not_block_valid_reference():
    store = Mock()
    rows = [watch(), watch(medusa_order_id="order_456", royal_mail_reference="AW-404")]
    store.due_orders.return_value = rows
    store.due_events.return_value = []
    reader = Mock()
    def lookup(references):
        if "AW-404" in references:
            raise PollHTTPError("Missing reference", status_code=400)
        return [rm_order()]
    reader.orders.side_effect = lookup
    result = run_cycle(store, reader=reader)
    assert result["observed"] == result["errors"] == 1
    assert reader.orders.call_count == 3


def test_observation_excludes_customer_data():
    raw = rm_order(recipient={"email": "private@example.com"})
    assert "recipient" not in observation(raw)


def test_tracking_packages_are_preserved_without_fake_urls():
    raw = rm_order(trackingNumber="CONSIGNMENT", packages=[
        {"packageNumber": 1, "trackingNumber": "PACKAGE1"},
        {"packageNumber": 2, "trackingNumber": "PACKAGE2"},
    ])
    payload = dispatch_payload("order_123", observation(raw))
    assert payload["labels"] == [
        {"tracking_number": "PACKAGE1", "tracking_url": "", "label_url": ""},
        {"tracking_number": "PACKAGE2", "tracking_url": "", "label_url": ""},
    ]
    assert "labels" not in dispatch_payload("order_123", observation(rm_order()))
    assert observation(rm_order(trackingNumber="ABC123     "))["trackingNumber"] == "ABC123"


def test_worker_splits_large_batches_at_one_hundred():
    store = Mock()
    store.due_orders.return_value = [watch(medusa_order_id=f"order_{i}", royal_mail_reference=f"AW-{i}") for i in range(205)]
    store.due_events.return_value = []
    reader = Mock()
    reader.orders.side_effect = lambda refs: [rm_order(orderReference=ref) for ref in refs]
    assert run_cycle(store, reader=reader)["observed"] == 205
    assert [len(call.args[0]) for call in reader.orders.call_args_list] == [100, 100, 5]


def test_lookup_batches_use_quoted_encoded_references_and_existing_auth(monkeypatch):
    get = Mock(return_value=Mock(status_code=200, json=lambda: []))
    monkeypatch.setattr(requests, "get", get)
    RoyalMailReader("https://api.parcel.royalmail.com/api/v1/", "secret").orders(["AW-1", "ref; /special"])
    args, kwargs = get.call_args
    assert args[0] == 'https://api.parcel.royalmail.com/api/v1/orders/"AW-1";"ref%3B%20%2Fspecial"'
    assert kwargs["headers"] == {"Authorization": "secret"}
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("size", [0, 101])
def test_invalid_batch_size_never_calls_royal_mail(size):
    with pytest.raises(ValueError): RoyalMailReader("http://example.com", "secret").orders(["ref"] * size)


def test_throttling_and_429_backoff(monkeypatch):
    stop = Mock()
    stop.wait.return_value = False
    reader = RoyalMailReader("https://example.com", "secret", stop)
    get = Mock(return_value=Mock(status_code=429, headers={"Retry-After": "90"}))
    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(PollHTTPError) as err: reader.orders(["AW-1"])
    assert err.value.retry_after == 90
    with pytest.raises(PollHTTPError): reader.orders(["AW-1"])
    assert get.call_count == 1
    assert stop.wait.call_count == 0
    reader.cooldown_until = 0
    with pytest.raises(PollHTTPError): reader.orders(["AW-1"])
    assert stop.wait.call_count == 1


def test_rate_limit_stops_all_remaining_batches():
    store = Mock()
    store.due_orders.return_value = [watch(medusa_order_id=f"order_{i}", royal_mail_reference=f"AW-{i}") for i in range(205)]
    store.due_events.return_value = []
    reader = Mock()
    reader.orders.side_effect = PollHTTPError("Rate limited", status_code=429, retry_after=120)
    assert run_cycle(store, reader=reader)["errors"] == 205
    assert reader.orders.call_count == 1
    assert store.poll_failure.call_count == 205


def test_dispatch_http_client_uses_internal_endpoint_and_key(monkeypatch):
    response = {"order_id": "order_123", "status": "shipped", "fulfillment_id": "ful_1"}
    post = Mock(return_value=Mock(status_code=200, json=lambda: response))
    monkeypatch.setattr(requests, "post", post)
    payload = {"order_id": "order_123", "status": "despatched"}
    assert ExporterDispatcher("http://127.0.0.1:8001/", "secret").send(payload) == response
    assert post.call_args.args == ("http://127.0.0.1:8001/orders/dispatch",)
    assert post.call_args.kwargs["headers"] == {"X-Integration-Key": "secret"}
    assert post.call_args.kwargs["json"] == payload


@pytest.mark.parametrize("code,detail,retryable", [(503, "Unavailable", True), (409, "Order needs review", False),
                                                   (409, "A dispatch request is in progress; retry shortly", True),
                                                   (403, "Unauthorized", False)])
def test_dispatch_errors_are_classified(monkeypatch, code, detail, retryable):
    monkeypatch.setattr(requests, "post", Mock(return_value=Mock(status_code=code, headers={}, json=lambda: {"detail": detail})))
    with pytest.raises(PollHTTPError) as err: ExporterDispatcher("http://localhost", "secret").send({"order_id": "order_123"})
    assert err.value.retryable is retryable


def test_cycle_is_observation_only_unless_both_switches_are_enabled():
    for apply, trigger in [(False, "disabled"), (True, "disabled"), (False, "shipped-on")]:
        store = Mock()
        store.due_orders.return_value = [watch(first_observed_at=datetime.now(timezone.utc), observation=observation(rm_order()))]
        store.due_events.return_value = []
        reader = Mock()
        reader.orders.return_value = [rm_order(shippedOn="2026-11-23T12:00:00Z")]
        result = run_cycle(store, reader=reader, apply=apply, trigger=trigger)
        assert result["detected"] == 1
        store.enqueue.assert_not_called()


def test_dry_run_does_not_consume_manual_alert():
    store = Mock()
    store.due_events.return_value = [{"event_key": "order_123"}]
    result = run_cycle(store)
    assert result["would_dispatch"] == 1
    store.attempted.assert_not_called()


def test_bad_order_does_not_prevent_other_order_detection():
    store = Mock()
    row = watch(first_observed_at=datetime.now(timezone.utc), observation=observation(rm_order()))
    store.due_orders.return_value = [watch(medusa_order_id="order_missing", royal_mail_reference="AW-MISSING"), row]
    store.due_events.return_value = []
    reader = Mock()
    reader.orders.return_value = [rm_order(shippedOn="2026-11-23T12:00:00Z")]
    result = run_cycle(store, reader=reader, apply=True, trigger="shipped-on")
    assert result["errors"] == result["queued"] == 1


def test_worker_does_not_need_royal_mail_credentials_for_manual_mode(monkeypatch):
    from contextlib import contextmanager
    store = Mock()
    store.due_events.return_value = []
    @contextmanager
    def memory_store(**_): yield store
    monkeypatch.setattr(poller, "polling_store", memory_store)
    monkeypatch.setattr(poller, "init_db", Mock())
    monkeypatch.setattr(poller, "init_polling_db", Mock())
    monkeypatch.setattr(poller, "load_dotenv", Mock())
    monkeypatch.delenv("ROYAL_MAIL_API_KEY", raising=False)
    monkeypatch.delenv("ROYAL_MAIL_BASE_URL", raising=False)
    assert poller.main(["--once", "--manual-only"]) == 0
    store.discover.assert_not_called()


def test_stop_signal_finishes_without_dispatching():
    from threading import Event
    stop = Event()
    stop.set()
    store = Mock()
    store.due_events.return_value = [{"event_key": "order_123"}]
    assert run_cycle(store, apply=True, stop=stop)["delivered"] == 0
    store.attempted.assert_not_called()
