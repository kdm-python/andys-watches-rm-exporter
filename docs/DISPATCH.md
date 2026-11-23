# Receive a dispatch notification

`POST /orders/dispatch` is the authenticated bridge from an owner-confirmed
Royal Mail dispatch to Medusa. It makes no Royal Mail API calls. A future
adapter can call this same contract. The [separate polling worker](POLLING.md)
now uses it, with automatic Royal Mail detection disabled by default. Label
generation is not a dispatch trigger.

## Configuration and startup

Use the existing `DB_URL`, `MEDUSA_BASE_URL`, `MEDUSA_ADMIN_API_KEY`, and
`INTEGRATION_API_KEY`. `DISPATCH_API_KEY` optionally sets a separate dispatch
credential; when set, it replaces the integration key for this endpoint only.
Both keys are supplied in the `X-Integration-Key` header. Nothing is logged
with either key's value.

Restart the exporter or let its development reloader restart it. Startup adds
the `royal_mail_dispatches` table alongside the existing export table. There
are no changes to existing export records or the `POST /orders` behavior.
The database user must have permission to create the new table, just as for
the original table. No Medusa backend code changes or deployment are needed.

For local development, from this repository:

```sh
uv run uvicorn rm_exporter.api:app --host 127.0.0.1 --port 8001 --reload
```

Do not start a second copy if the exporter is already listening on port 8001.
Uvicorn does not automatically use the `.env` file's `PORT`; pass the port.

## Manual test

Replace `YOUR_INTEGRATION_API_KEY` with the existing integration key, or the
dispatch key if separately configured. This request changes the real order
and asks Medusa to send its dispatch email:

```sh
curl --fail-with-body -i \
  -X POST http://localhost:8001/orders/dispatch \
  -H 'Content-Type: application/json' \
  -H 'X-Integration-Key: YOUR_INTEGRATION_API_KEY' \
  --data '{"order_id":"order_01M7TGR7M1G2NSN9PF99BNQFTH","status":"despatched"}'
```

Expected first response (the fulfillment ID is assigned by Medusa):

```json
{
  "order_id": "order_01M7TGR7M1G2NSN9PF99BNQFTH",
  "fulfillment_id": "ful_...",
  "status": "shipped",
  "already_shipped": false
}
```

Repeat the same request to verify `already_shipped: true`. The repeated
request does not create another shipment or request another email. A 200
confirms shipping state, not inbox delivery: Medusa's worker processes the
existing `shipment.created` email subscriber asynchronously. If the order
was already shipped manually, no additional email is requested.

The sample order was inspected read-only during implementation: payment
captured, no fulfillments, Royal Mail Tracked 24 option, `manual_manual`
provider, and Home Office location. Its line currently has
`requires_shipping: false`; the bridge deliberately does not drop such
lines, matching Medusa's fulfillment behavior. That product configuration
can be reviewed separately.

## Request contract

`order_id` is the Medusa order ID, not a Click & Drop identifier or the
`AW-...` display reference. `status` must be exactly `despatched`; values
such as `label_generated` are rejected with 422. The caller is responsible
for establishing that the owner actually marked the Royal Mail order
Despatched; the endpoint cannot verify that event's origin at this stage.

Order-level dispatch assumes one parcel covering the complete order. It
creates a fulfillment when none exists, or reuses the sole active full
fulfillment. Multiple or partial fulfillments require an explicit
`fulfillment_id` belonging to the order; that ships only the selected
fulfillment. This leaves room for multiple parcels without automatically
shipping every fulfillment on an ambiguous request.

Optional tracking is an array, for example:

```json
{
  "order_id": "order_01M7TGR7M1G2NSN9PF99BNQFTH",
  "status": "despatched",
  "labels": [
    {
      "tracking_number": "YOUR_REAL_TRACKING_NUMBER",
      "tracking_url": "https://www.royalmail.com/track-your-item#/tracking-results/YOUR_REAL_TRACKING_NUMBER"
    }
  ]
}
```

`tracking_url` and `label_url` default to empty strings (accepted by Medusa
2.17.0), so a tracking number alone also works. Nonempty URLs must use
HTTP(S). Omitted or empty labels preserve existing fulfillment tracking.
Do not send a private postage-label URL to customers. Supplying different
tracking on a replay returns 409 instead of silently changing the shipment.

## Medusa workflow and safeguards

The bridge uses the existing secret Admin API key in `Authorization: Basic`:

1. `GET /admin/orders/{order_id}` with fulfillment, item-detail, and inventory
   fields; resolve the selected shipping option and location.
2. If necessary, `POST /admin/orders/{order_id}/fulfillments`, with the
   purchased line-item IDs and quantities. `no_notification: true` suppresses
   a separate fulfillment/packing notice.
3. `POST /admin/orders/{order_id}/fulfillments/{fulfillment_id}/shipments`,
   with shipped items, optional labels, and `no_notification: false`.
   Medusa runs `createOrderShipmentWorkflow`, updates its shipping records,
   and emits the real `shipment.created` event used by the working email.

The native Medusa fulfillment workflow handles stock/reservation updates.
Inventory kits are normalized back to purchased quantities for shipments.
The verified `manual_manual` provider performs no carrier booking or label
purchase. Other providers, pickup options, unpaid orders, orders with
returns/written-off items, and ambiguous fulfillment arrangements are
rejected rather than guessed. Fulfillment creation with mixed shipping
requirements must be handled manually.

A PostgreSQL session advisory lock per order prevents simultaneous exporter
workers from dispatching it twice. Progress is committed before each HTTP
write; no database transaction is kept open across network calls. Closing
the connection releases the lock, including after failures or restarts.

## Errors and recovery

| HTTP | Meaning |
| --- | --- |
| 200 | Shipment created, or the selected fulfillment was already shipped. |
| 403 | Wrong integration/dispatch key. |
| 422 | Missing header or invalid input, including a non-dispatch event. |
| 404 | Medusa could not find the requested resource. |
| 409 | Another request is running, state is ambiguous, tracking conflicts, or the order needs manual attention. The detail explains why. |
| 502 | Medusa connection/upstream failure, including an invalid Medusa admin key. |
| 503 | Missing configuration or unavailable dispatch database. |
| 504 | Medusa request timeout. |

A timeout can occur after Medusa committed the operation. Retry the same
request: if a fulfillment was created it is reused, and if shipment already
completed it returns `already_shipped: true`. If a previous write remains
uncertain and Medusa does not yet show its result, the endpoint returns 409
and logs the order ID instead of issuing another write. Explicitly selecting
a fulfillment cannot bypass another unresolved request for the same order.

For an unresolved write, inspect Medusa and its worker/server logs. Wait for
any running workflow to complete before retrying. If it definitively failed,
finish the fulfillment or shipment through Medusa Admin, then retry to
reconcile. Do not blindly delete dispatch records or repeat the native POST.
Known Medusa validation errors permit a retry after the underlying issue
(such as a stock reservation) is corrected.

An SMTP failure is separate from dispatch state. Use the backend's existing
`retry-dispatch-email` command for a failed notification; do not create a new
shipment to resend an email.

## Verification

The dispatch suite covers native Admin request contracts, auth/input errors,
existing/new/partial fulfillments, tracking, kits, duplicate/replayed calls,
known failures, ambiguous timeouts, persistence, and real PostgreSQL lock
contention including concurrent HTTP dispatch calls. Run it against a
disposable database:

```sh
TEST_DATABASE_URL=postgresql://user:password@localhost/test_database \
  uv run pytest -q tests/test_dispatch.py tests/test_dispatch_store.py tests/test_api.py tests/test_db.py tests/test_medusa.py
```

Database tests use temporary isolated schemas and never use `DB_URL` as their
test database. Outbound HTTP is mocked in tests. The real sample-order checks
were GET-only; the final Admin/worker/inbox test is the manual curl above.

References: [Medusa order SDK/API methods](https://docs.medusajs.com/resources/references/js-sdk/admin/order),
[shipment event](https://docs.medusajs.com/resources/references/fulfillment/events).
The request bodies were also checked against the locally installed Medusa
2.17.0 Admin route validators and workflow source.
