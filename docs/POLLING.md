# Polling worker and local tests

The polling code lives in this repository and runs as a **separate foreground
process** from FastAPI.
The eventual LXC service can run the same `rm-poller` command with an
EnvironmentFile, a working directory, and systemd restart/logging settings.

The complete path is:

1. A manual alert or an explicitly enabled Royal Mail detector puts a
   `despatched` event in a durable PostgreSQL inbox.
2. The separate worker posts it to the existing authenticated
   `/orders/dispatch` endpoint.
3. That endpoint creates/reuses the Medusa fulfillment and creates its
   shipment. The existing Medusa worker sends the dispatch email.

Receiving an inbox event does not itself change Medusa. The poller owns a small
liveness listener; inbox/status routes still belong to the exporter API.

## Installation and configuration

```sh
uv sync
uv run rm-poller --help
```

No new dependencies were needed. The worker uses the existing requests,
psycopg2, dotenv, and validation packages. `python -m rm_exporter.poller` is
also supported in the installed environment.

The API and worker must share `DB_URL`. Startup creates two additional
tables: `royal_mail_polling` (observations) and `royal_mail_poll_inbox`
(delivery progress). Existing export and Medusa dispatch records are not
rewritten. The database user needs permission to create these tables.

The worker reads the same `.env` file as the exporter; environment variables
already set by the shell/systemd take precedence. Configuration:

| Variable | Purpose |
| --- | --- |
| `DB_URL` | Shared exporter database. Required in all modes. |
| `POLLING_ACTIVE` | `true` (default when omitted) enables Click & Drop reads; `false` skips them while continuing inbox processing. Case-insensitive; other values fail startup. Read at startup, so restart after changes. |
| `RM_EXPORTER_BASE_URL` | Internal API destination, e.g. `http://127.0.0.1:8001` locally. Defaults to loopback and `PORT` (or 8001). Set this explicitly for the later LXC service. |
| `DISPATCH_API_KEY` / `INTEGRATION_API_KEY` | Dispatch key takes precedence; same key used by the existing dispatch endpoint. Required with `--apply`. |
| `ROYAL_MAIL_BASE_URL` / `ROYAL_MAIL_API_KEY` | Existing public Click & Drop API configuration. Not needed with `--manual-only`. |
| `POLLER_HEALTH_HOST` | Liveness listener address. Defaults to `0.0.0.0`. |
| `POLLER_HEALTH_PORT` | Liveness listener port. Defaults to `8002`. |

While the continuous poller process is running, `GET /health/live` on that
listener returns HTTP 200 with
`{"status":"ok","service":"royal-mail-poller"}`. For example:

```sh
curl --fail-with-body http://rm-click-drop.home:8002/health/live
```

This only proves that the poller process is running and accepting requests. It
does not prove that a polling cycle or Click & Drop request succeeded.

`GET /health/ready` on the same listener reports polling-cycle freshness. It
returns HTTP 200 only when polling is enabled, the latest cycle completed
without Royal Mail observation errors, and that completion is no older than
three configured polling intervals (with a minimum allowance of the interval
plus 30 seconds). It returns HTTP 503 while starting, when polling is disabled,
after a cycle with errors, or when the last completion is stale.
An initial cycle that starts but never completes also becomes stale.

```sh
curl --fail-with-body http://rm-click-drop.home:8002/health/ready
```

The response includes the configured interval and freshness limit, UTC times
for the last cycle start, completion, and success, the cycle age, and the last
cycle's error count. A cycle with no due orders is a successful completed
cycle. No order or customer details are exposed.

`GET /health/click-and-drop` reports the cached result of a separate,
authenticated, read-only Click & Drop connectivity check. Once per polling
cycle the worker calls `GET /carriers`, validates the returned JSON shape, and
caches only the result, time, and HTTP status; it does not retain or expose the
carrier response. Royal Mail documents this as an account-authenticated read
operation, so the check creates, changes, dispatches, and deletes nothing.

```sh
curl --fail-with-body http://rm-click-drop.home:8002/health/click-and-drop
```

The endpoint returns HTTP 200 while the most recent check succeeded and is no
older than the same freshness limit used by the polling-cycle check. It returns
HTTP 503 while starting, after an error, when stale, or when Click & Drop
polling is disabled. Kuma requests only the cached local result and therefore
do not generate extra Click & Drop traffic.

Medusa credentials remain on the exporter API side. The worker itself posts
to the existing bridge; it does not reimplement shipment creation.

With `POLLING_ACTIVE=false`, run `uv run rm-poller --apply` to continuously
deliver manually queued alerts through Medusa/email without contacting Click &
Drop. Royal Mail credentials are not required in this mode. Existing queued
events, including previously detected Royal Mail events, can still be delivered.
Without `--apply`, the worker only reports `would_dispatch`.
This switch controls the separate worker's reads; the existing order export
and authenticated dispatch/inbox endpoints continue operating normally.

For a single read-only Click & Drop check while `.env` contains `false`, use:

```sh
POLLING_ACTIVE=true uv run rm-poller --once --order-id order_REPLACE_ME
```

This does not send dispatches. For continuous automatic detection and delivery,
set `POLLING_ACTIVE=true`, restart the worker, and use `--apply --trigger shipped-on`
only after verifying the account's `shippedOn` behaviour. `--manual-only` always
skips Royal Mail reads, even when `POLLING_ACTIVE=true`.

## 1. Test the inbox and worker without Royal Mail

Keep the existing API running on port 8001. If it is not running:

```sh
uv run uvicorn rm_exporter.api:app --host 127.0.0.1 --port 8001 --reload
```

Use a **new captured, unshipped Medusa order** to test a new email. The old
sample `order_01M7TGR7M1G2NSN9PF99BNQFTH` has already been shipped and was used
to verify reconciliation; it will not send another dispatch email.

Replace `order_REPLACE_ME` and the key in the commands below. If you set a
separate `DISPATCH_API_KEY`, use that key in place of the integration key.

```sh
curl --fail-with-body -i \
  -X POST http://localhost:8001/polling/dispatch \
  -H 'Content-Type: application/json' \
  -H 'X-Integration-Key: YOUR_INTEGRATION_API_KEY' \
  --data '{"order_id":"order_REPLACE_ME","status":"despatched"}'
```

Expect **202**, an `event_key`, and `status: pending`. `label_generated` and
other statuses are rejected. Optional `fulfillment_id` and `labels` follow
the [existing dispatch contract](DISPATCH.md). This is a trusted manual
test alert, not a simulated claim that Royal Mail actually dispatched it.

First inspect what would happen, without sending:

```sh
uv run rm-poller --once --manual-only --order-id order_REPLACE_ME
```

Expect `would_dispatch: 1`. Then send that order's queued event:

```sh
uv run rm-poller --once --manual-only --apply --order-id order_REPLACE_ME
```

Expect `delivered: 1`. The real dispatch/email flow now runs. Check status:

```sh
curl --fail-with-body \
  http://localhost:8001/polling/events/order_REPLACE_ME \
  -H 'X-Integration-Key: YOUR_INTEGRATION_API_KEY'
```

The response includes status, attempts, last error, and the Medusa bridge
result. `delivered` means the exporter confirmed shipped state, not inbox
delivery of the email; check the Medusa worker and customer/test inbox.
Posting the same alert again and rerunning the worker does not resend it.
A different payload for an existing event key returns 409.

To run continuously against only this manual test order, omit `--once`:

```sh
uv run rm-poller --manual-only --apply --order-id order_REPLACE_ME
```

## 2. Observe the personal-account API

The live personal-account tests confirmed that the ordinary order-list and
reference-lookup GET endpoints work with the existing API key. A current
order had `printedOn` populated while `shippedOn` was null. The account's
timestamps can omit timezone offsets; these are preserved without assuming
UTC, since the detector compares state rather than calculating elapsed time.
Tracking numbers are trimmed because the live response padded one with spaces.

The old stored `AW-33` reference and Royal Mail ID 2054 now return “does not
exist.” That is not an authentication/feature failure. Missing references
are retried with backoff; a missing reference cannot make an entire batch of
valid references unobservable.

Create/export a fresh order through the usual workflow, **before** the owner
marks it Despatched. The outbound export must have recorded it as `sent` in
this same database. Then run:

```sh
uv run rm-poller --order-id order_REPLACE_ME --interval 10
```

This logs/persists observations every ten seconds but sends **nothing** to
Medusa. Ctrl+C stops cleanly. For one cycle, add `--once`; existing next-check
times still apply, so an immediate second run can have `observed: 0`.

Record the fields in the worker logs at these stages:

1. Imported/new order.
2. Label generated, before manual Despatched.
3. Owner manually marks it **Despatched**.
4. Manifested, if the personal account exposes/uses that stage.

We still need to establish that `shippedOn` changes at step 3 and not step 2
or automatic manifesting. `printedOn`, `manifestedOn`, and tracking numbers
alone **never** trigger dispatch. The adapter deliberately uses the ordinary
lookup, not the ChannelShipper-only `/full` endpoint.

## 3. Enable automatic detection only after the above check

Royal Mail detection is disabled by default, even with `--apply`. To enable
the candidate `shippedOn` rule explicitly for the controlled order:

```sh
uv run rm-poller --apply --trigger shipped-on --order-id order_REPLACE_ME --interval 10
```

This requires BOTH switches: `--apply` allows inbox delivery, and
`--trigger shipped-on` allows observations to enqueue Royal Mail events.
`--apply` on its own still delivers explicit manual alerts.

The first observation is a baseline. An order already shipped on first
observation is held as `historical`, not sent automatically. Use an explicit
manual inbox alert if you intentionally want to synchronize that order.
For orders first seen unshipped, a later nonempty `shippedOn` becomes
`detected`. A transition recorded during observation-only testing remains
eligible when you explicitly enable BOTH switches later; it need not occur
again. There is no automatic reversal of shipping state.

After the controlled test succeeds, a future service can run without the
order filter using the default 60-second interval. Do not enable that until
you intend all eligible tracked orders and pending manual alerts to be sent.

## Durability, failures, and service behavior

- Only one polling cycle owns the database advisory lock at a time, across
  processes. Manual HTTP alerts remain available while the worker runs.
- Up to 1,000 due orders/events are handled per cycle, in Royal Mail batches
  of at most 100 references. Requests use the documented quoted/URL-encoded
  reference format and existing `Authorization` API key header.
- Royal Mail GETs are limited to one per second to leave export headroom.
  If a bad reference rejects a batch with 400/404, the worker bisects it,
  using the same throttle. Missing/ambiguous results never dispatch an order.
- Rechecks use the stored outstanding references, not a “recently created”
  window, so an older order dispatched later is not missed.
- Temporary failures back off exponentially up to one hour. Numeric
  `Retry-After` values are respected. One bad order/event does not stop others.
- Changed Royal Mail identifiers or a changed/reset `shippedOn` require
  review. Existing dispatch endpoint safeguards also remain active.
- Queue delivery is at least once. If the worker stops after an HTTP success
  but before recording it, replay goes through the existing idempotent bridge.
  It does not create another shipment or resend a successful notification.
- Delivered events are not sent again. Permanent input/auth/order conflicts
  are held as `review`; temporary downstream errors become `retry`.
- After correcting an error, requeue an unfinished event explicitly:

  ```sh
  uv run rm-poller --retry-event order_REPLACE_ME
  uv run rm-poller --once --manual-only --apply --order-id order_REPLACE_ME
  ```

  Stop another active worker or retry the command if it owns the cycle lock.
  Completed events cannot be requeued. An ambiguous Medusa write still needs
  the [existing dispatch reconciliation procedure](DISPATCH.md).
- SIGTERM/SIGINT finish an active operation and stop the foreground process;
  there is no background child or web-server lifecycle hook. Later systemd
  shutdown time should allow an in-flight HTTP call (up to 90 seconds).
- Logs go to stderr and each cycle writes a compact JSON summary to stdout.
  API keys and customer addresses/emails are not logged. Observations include
  only references, IDs, dispatch/print/manifest timestamps, and tracking.
- `--once` exits 0 on a clear cycle and 1 on errors/retries/review or an
  occupied worker lock. `historical` observations count as review. Invalid
  CLI configuration exits 2. Continuous mode retries transient database
  failures; startup failures exit so a future service manager can restart it.

## What is tested / what remains

Automated tests cover initial baselines, label-only and manifest-only changes,
dispatch transitions, history suppression, tracking, personal-account date
formats, batch sizing/encoding/isolation, auth, durable inbox replay, retry
backoff, order filters, exclusive worker locking, and inbox → worker → real
dispatch handler integration (Medusa is simulated in that test).

A real local smoke test queued the already-shipped sample order, launched a
separate worker process, called the real exporter/Medusa API, and recorded
`delivered` / `already_shipped: true`. It generated no extra email. A separate
foreground worker also exited successfully on SIGTERM. Royal Mail GETs were
tested on a current personal-account order.

The remaining account-specific evidence is the same order's before/after
`shippedOn` behavior for manual dispatch, label generation, and manifesting.
If Royal Mail also sets it automatically, do not enable `--trigger shipped-on`;
we need a different observable signal or an explicit owner alert. No library
or deployment change can establish that business distinction on its own.

Tests use a disposable PostgreSQL database, with isolated schemas:

```sh
TEST_DATABASE_URL=postgresql://user:password@localhost/test_database \
  uv run pytest -q tests/test_polling.py tests/test_polling_store.py \
  tests/test_dispatch.py tests/test_dispatch_store.py tests/test_api.py \
  tests/test_db.py tests/test_medusa.py
```

Reference: [Click & Drop public API specification](https://api.parcel.royalmail.com/doc/v1/click-and-drop-api-v1.yaml).
