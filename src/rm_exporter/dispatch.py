"""Use Medusa's order workflows; never update a status column or emit a fake event."""

from decimal import Decimal, InvalidOperation

import requests
from loguru import logger


class DispatchError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class MedusaError(DispatchError):
    def __init__(
        self, status_code: int, detail: str, upstream_status: int | None = None
    ):
        self.upstream_status = upstream_status
        super().__init__(status_code, detail)


DISPATCH_FIELDS = ",".join(
    [  # noqa: FLY002
        "+payment_status",
        "+fulfillment_status",
        "+items.*",
        "+items.detail.*",
        "+items.variant.inventory_items.inventory.id",
        "+items.variant.inventory_items.required_quantity",
        "+shipping_methods.*",
        "+fulfillments.*",
        "+fulfillments.items.*",
        "+fulfillments.labels.*",
    ]
)


class MedusaDispatchClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Basic {api_key}"}

    def request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                headers=self.headers,
                timeout=(5, 30),
                allow_redirects=False,
                **kwargs,
            )
        except requests.Timeout as exc:
            raise MedusaError(
                504, "Medusa timed out; retry to reconcile the order state"
            ) from exc
        except requests.RequestException as exc:
            raise MedusaError(
                502, "Medusa connection failed; retry to reconcile the order state"
            ) from exc
        if not 200 <= response.status_code < 300:
            code = response.status_code
            mapped = 404 if code == 404 else 409 if code in (400, 409, 422) else 502
            try:
                message = str(response.json().get("message", "Request rejected"))[:500]
            except (ValueError, AttributeError):
                message = "Request rejected"
            raise MedusaError(mapped, f"Medusa returned HTTP {code}: {message}", code)
        try:
            data = response.json()
        except ValueError as exc:
            raise MedusaError(
                502, "Medusa returned invalid JSON; retry to reconcile the order state"
            ) from exc
        if not isinstance(data, dict):
            raise MedusaError(
                502,
                "Medusa returned an unexpected response; retry to reconcile the order state",
            )
        return data

    def order(self, order_id: str) -> dict:
        data = self.request(
            "GET", f"/admin/orders/{order_id}", params={"fields": DISPATCH_FIELDS}
        )
        if not isinstance(data.get("order"), dict):
            raise MedusaError(502, "Medusa response is missing the order")
        return data["order"]

    def shipping_option(self, option_id: str) -> dict:
        data = self.request(
            "GET",
            f"/admin/shipping-options/{option_id}",
            params={
                "fields": "+service_zone.fulfillment_set.location.id,+type.*",
            },
        )
        if not isinstance(data.get("shipping_option"), dict):
            raise MedusaError(502, "Medusa response is missing the shipping option")
        return data["shipping_option"]

    def create_fulfillment(self, order_id: str, items: list[dict], option: dict):
        self.request(
            "POST",
            f"/admin/orders/{order_id}/fulfillments",
            json={
                "items": items,
                "shipping_option_id": option["id"],
                "location_id": option["service_zone"]["fulfillment_set"]["location"][
                    "id"
                ],
                # Suppress a separate packing email; shipment notification stays on.
                "no_notification": True,
            },
        )

    def ship(
        self, order_id: str, fulfillment_id: str, items: list[dict], labels: list[dict]
    ):
        self.request(
            "POST",
            f"/admin/orders/{order_id}/fulfillments/{fulfillment_id}/shipments",
            json={
                "items": items,
                "labels": labels,
                "no_notification": False,
            },
        )


def number(value) -> Decimal:
    try:
        result = Decimal(str(value))
        if result.is_finite() and result >= 0:
            return result
    except (InvalidOperation, ValueError):
        pass
    raise DispatchError(409, "Order contains an invalid or missing quantity")


def purchased_items(order: dict) -> dict[str, dict]:
    items = {item["id"]: item for item in order.get("items", [])}
    if not items or any(number(item.get("quantity")) <= 0 for item in items.values()):
        raise DispatchError(409, "Order has no valid items to dispatch")
    for item in items.values():
        detail = item.get("detail") or {}
        # These order-module quantities are required for safe resume/reconciliation.
        number(detail.get("fulfilled_quantity"))
        number(detail.get("shipped_quantity"))
        if any(
            number(detail.get(field, 0)) > 0
            for field in (
                "return_requested_quantity",
                "return_received_quantity",
                "written_off_quantity",
            )
        ):
            raise DispatchError(
                409, "Order has returns or written-off items; dispatch it manually"
            )
    return items


def shipment_items(fulfillment: dict, items: dict[str, dict]) -> list[dict]:
    quantities = {}
    for row in fulfillment.get("items", []):
        item_id = row.get("line_item_id")
        if item_id not in items:
            raise DispatchError(409, "Fulfillment contains items outside this order")
        quantity = number(row.get("quantity"))
        inventory_id = row.get("inventory_item_id")
        if inventory_id:
            inventory = (items[item_id].get("variant") or {}).get(
                "inventory_items"
            ) or []
            link = next(
                (
                    entry
                    for entry in inventory
                    if (entry.get("inventory") or {}).get("id") == inventory_id
                ),
                None,
            )
            required = number(link.get("required_quantity") if link else None)
            if required <= 0:
                raise DispatchError(409, "Invalid inventory quantity in fulfillment")
            quantity /= required
        if quantity <= 0 or quantity > number(items[item_id]["quantity"]):
            raise DispatchError(409, "Invalid shipment item quantity")
        # Multiple inventory components of one purchased kit are not extra products.
        if item_id in quantities and quantities[item_id] != quantity:
            raise DispatchError(
                409, "Inventory kit has inconsistent fulfillment quantities"
            )
        quantities[item_id] = quantity
    if not quantities:
        raise DispatchError(409, "Fulfillment has no items")
    return [{"id": key, "quantity": float(value)} for key, value in quantities.items()]


def check_provider(option: dict):
    if option.get("provider_id") != "manual_manual":
        raise DispatchError(
            409,
            "Only the verified manual_manual provider is supported; no carrier booking was attempted",
        )
    if (option.get("type") or {}).get("code") == "pickup":
        raise DispatchError(409, "Pickup orders cannot be dispatched as parcels")


def dispatch_order(
    client: MedusaDispatchClient,
    store,
    order_id: str,
    fulfillment_id: str | None = None,
    labels: list[dict] | None = None,
) -> dict:
    labels = labels or None
    # An explicit fulfillment ID leaves room for multiple parcels later, without
    # interpreting an ambiguous order-level request as permission to ship all.
    key = f"{order_id}:{fulfillment_id}" if fulfillment_id else order_id
    record = store.get(key)
    if record and labels is not None and record["labels"] != labels:
        raise DispatchError(
            409, "Tracking labels differ from the original dispatch request"
        )
    order = client.order(order_id)
    if order.get("status") in ("canceled", "draft", "archived"):
        raise DispatchError(409, "Order is canceled, draft, or archived")
    active = [f for f in order.get("fulfillments", []) if not f.get("canceled_at")]
    target_id = fulfillment_id or (record or {}).get("fulfillment_id")
    if target_id:
        target = next((f for f in active if f["id"] == target_id), None)
        if not target:
            raise DispatchError(
                409,
                "Requested fulfillment is missing, canceled, or belongs to another order",
            )
    elif len(active) > 1:
        raise DispatchError(
            409, "Order has multiple fulfillments; supply fulfillment_id"
        )
    else:
        target = active[0] if active else None

    if target and (target.get("shipped_at") or target.get("delivered_at")):
        if labels is not None:
            actual = [
                {
                    k: row.get(k) or ""
                    for k in ("tracking_number", "tracking_url", "label_url")
                }
                for row in target.get("labels", [])
            ]
            if labels != actual:
                raise DispatchError(
                    409, "Shipment already exists with different tracking labels"
                )
        if record:
            store.update(key, "shipped", target["id"])
        return {
            "order_id": order_id,
            "fulfillment_id": target["id"],
            "status": "shipped",
            "already_shipped": True,
        }

    if order.get("payment_status") != "captured":
        raise DispatchError(409, "Order payment must be captured before dispatch")
    if store.has_other_unresolved(order_id, key):
        raise DispatchError(
            409,
            "Another dispatch request for this order is unresolved; inspect Medusa before proceeding",
        )
    items = purchased_items(order)
    if not fulfillment_id:
        # The order-only v1 request means the complete order, not a partial parcel.
        expected = {key: number(item["quantity"]) for key, item in items.items()}
        if (
            target
            and {i["id"]: number(i["quantity"]) for i in shipment_items(target, items)}
            != expected
        ):
            raise DispatchError(
                409,
                "Existing fulfillment covers only part of the order; supply fulfillment_id",
            )

    if record and record["status"] == "shipped":
        raise DispatchError(
            409, "Recorded shipment conflicts with Medusa; review manually"
        )
    if not target and record and record["status"] != "pending":
        raise DispatchError(
            409,
            "Previous fulfillment request is unresolved; inspect Medusa and retry after it completes. No second fulfillment was created",
        )
    if target and record and record["status"] == "shipment_requested":
        raise DispatchError(
            409,
            "Previous shipment request is unresolved; inspect Medusa and retry after it completes. No second shipment was created",
        )

    if target:
        if target.get("provider_id") != "manual_manual":
            raise DispatchError(409, "Existing fulfillment does not use manual_manual")
        option_id = target.get("shipping_option_id")
    else:
        if any(
            number(item["detail"]["fulfilled_quantity"]) > 0 for item in items.values()
        ):
            raise DispatchError(
                409,
                "Order already has fulfilled quantities; review fulfillments manually",
            )
        if len({bool(item.get("requires_shipping")) for item in items.values()}) > 1:
            raise DispatchError(
                409, "Mixed shipping requirements need separate manual fulfillments"
            )
        methods = order.get("shipping_methods") or []
        if len(methods) != 1:
            raise DispatchError(
                409, "Order must have one shipping method for order-level dispatch"
            )
        option_id = methods[0].get("shipping_option_id")
    if not option_id:
        raise DispatchError(
            409, "No shipping option is associated with the order or fulfillment"
        )
    option = client.shipping_option(option_id)
    check_provider(option)
    location = ((option.get("service_zone") or {}).get("fulfillment_set") or {}).get(
        "location"
    ) or {}
    if not target and not location.get("id"):
        raise DispatchError(409, "Shipping option is not linked to a stock location")

    record = record or store.create(key, order_id, labels or [])
    if not target:
        body_items = [
            {"id": key, "quantity": float(number(item["quantity"]))}
            for key, item in items.items()
        ]
        store.update(key, "fulfillment_requested")
        logger.info("Creating Medusa fulfillment for order {}", order_id)
        try:
            client.create_fulfillment(order_id, body_items, option)
        except MedusaError as exc:
            if exc.upstream_status in (400, 401, 403, 404, 409, 422):
                store.update(key, "pending")
            raise
        order = client.order(order_id)
        active = [f for f in order.get("fulfillments", []) if not f.get("canceled_at")]
        if len(active) != 1:
            raise DispatchError(
                409, "Cannot identify the newly created fulfillment; inspect Medusa"
            )
        target = active[0]
        if target.get("shipped_at") or target.get("delivered_at"):
            store.update(key, "shipped", target["id"])
            return {
                "order_id": order_id,
                "fulfillment_id": target["id"],
                "status": "shipped",
                "already_shipped": True,
            }

    body_items = shipment_items(target, items)
    store.update(key, "fulfillment_created", target["id"])
    # Preserve existing labels when the caller supplies no tracking information.
    shipment_labels = record["labels"] or [
        {k: row.get(k) or "" for k in ("tracking_number", "tracking_url", "label_url")}
        for row in target.get("labels", [])
    ]
    store.update(key, "shipment_requested", target["id"])
    logger.info(
        "Marking Medusa fulfillment {} shipped for order {}", target["id"], order_id
    )
    try:
        client.ship(order_id, target["id"], body_items, shipment_labels)
    except MedusaError as exc:
        if exc.upstream_status in (400, 401, 403, 404, 409, 422):
            store.update(key, "fulfillment_created", target["id"])
        raise
    store.update(key, "shipped", target["id"])
    return {
        "order_id": order_id,
        "fulfillment_id": target["id"],
        "status": "shipped",
        "already_shipped": False,
    }
