"""Foreground worker for a future separate systemd service; also supports --once."""

import argparse
import json
import os
import signal
from collections import deque
from threading import Event

import psycopg2
import requests
from dotenv import load_dotenv
from loguru import logger
from pydantic import ValidationError

from rm_exporter.db import init_db
from rm_exporter.poll_store import (
    PollerBusy,
    QueueConflict,
    init_polling_db,
    polling_store,
)
from rm_exporter.royal_mail_polling import (
    PollHTTPError,
    RoyalMailReader,
    classify,
    dispatch_payload,
    observation,
    retry_after,
)


def backoff(attempts):
    return min(3600, 30 * 2 ** min(attempts, 7))


class ExporterDispatcher:
    def __init__(self, base_url, api_key):
        self.url = base_url.rstrip("/") + "/orders/dispatch"
        self.api_key = api_key

    def send(self, payload):
        try:
            response = requests.post(
                self.url,
                json=payload,
                headers={"X-Integration-Key": self.api_key},
                timeout=(5, 90),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PollHTTPError(
                "Exporter connection failed or timed out; retry will reconcile Medusa"
            ) from exc
        if response.status_code != 200:
            # An order lock conflict is transient; other conflicts need review.
            try:
                detail = str(response.json().get("detail", ""))[:500]
            except (ValueError, AttributeError):
                detail = ""
            busy = (
                response.status_code == 409 and "in progress; retry shortly" in detail
            )
            transient = (
                busy or response.status_code >= 500 or response.status_code == 429
            )
            raise PollHTTPError(
                f"Exporter returned HTTP {response.status_code}: {detail}",
                retryable=transient,
                retry_after=retry_after(response),
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise PollHTTPError("Exporter returned invalid JSON") from exc
        if (
            not isinstance(result, dict)
            or result.get("order_id") != payload["order_id"]
            or result.get("status") != "shipped"
            or not result.get("fulfillment_id")
        ):
            raise PollHTTPError("Exporter returned an unexpected dispatch result")
        return result


def run_cycle(
    store,
    *,
    reader=None,
    dispatcher=None,
    apply=False,
    trigger="disabled",
    order_id=None,
    interval=60,
    stop=None,
):
    stop = stop or Event()
    counts = {
        key: 0
        for key in (
            "observed",
            "detected",
            "queued",
            "delivered",
            "retry",
            "review",
            "errors",
            "would_dispatch",
        )
    }
    if reader is not None:
        store.discover(order_id)
        due = store.due_orders(order_id)
        batches = deque(
            due[offset : offset + 100] for offset in range(0, len(due), 100)
        )
        while batches:
            if stop.is_set():
                break
            batch = batches.popleft()
            try:
                rows = reader.orders([row["royal_mail_reference"] for row in batch])
            except PollHTTPError as exc:
                if exc.status_code in (400, 404) and len(batch) > 1:
                    # A deleted/invalid reference can reject an entire Royal Mail
                    # batch. Bisect it so valid orders still get checked. The
                    # reader's shared one-request/second throttle still applies.
                    middle = len(batch) // 2
                    batches.appendleft(batch[middle:])
                    batches.appendleft(batch[:middle])
                    continue
                logger.warning("{}", exc)
                if exc.status_code == 429:
                    # Retry-After applies to the API, not just this batch.
                    batch = batch + [row for pending in batches for row in pending]
                    batches.clear()
                for row in batch:
                    store.poll_failure(
                        row, str(exc), max(backoff(row["failures"]), exc.retry_after)
                    )
                    counts["errors"] += 1
                continue
            for row in batch:
                try:
                    matches = [
                        r
                        for r in rows
                        if r.get("orderReference") == row["royal_mail_reference"]
                    ]
                    if len(matches) != 1:
                        raise PollHTTPError(
                            "Royal Mail reference was missing or ambiguous; nothing dispatched"
                        )
                    current = observation(matches[0])
                    status = classify(row, current)
                    logger.info(
                        "Royal Mail observation for {}: {} ({})",
                        row["medusa_order_id"],
                        json.dumps(current),
                        status,
                    )
                    counts["observed"] += 1
                    if status == "detected":
                        counts["detected"] += 1
                        if apply and trigger == "shipped-on":
                            store.enqueue(
                                dispatch_payload(row["medusa_order_id"], current),
                                "royal-mail",
                            )
                            status = "queued"
                            counts["queued"] += 1
                    if status in ("review", "historical"):
                        counts["review"] += 1
                    store.observe(row, current, status, interval)
                except (
                    PollHTTPError,
                    QueueConflict,
                    ValidationError,
                    TypeError,
                ) as exc:
                    # A bad response for one order must not prevent checking others.
                    message = (
                        str(exc)
                        if isinstance(exc, (PollHTTPError, QueueConflict))
                        else "Invalid Royal Mail tracking data"
                    )
                    logger.warning(
                        "Royal Mail observation rejected for {}: {}",
                        row["medusa_order_id"],
                        message,
                    )
                    store.poll_failure(row, message, backoff(row["failures"]))
                    counts["errors"] += 1
    for event in store.due_events(order_id):
        if stop.is_set():
            break
        if not apply:
            counts["would_dispatch"] += 1
            logger.info(
                "Would dispatch queued event {} (use --apply to send)",
                event["event_key"],
            )
            continue
        store.attempted(event["event_key"])
        try:
            result = dispatcher.send(event["payload"])
            store.event_result(event["event_key"], "delivered", result=result)
            logger.info(
                "Delivered dispatch event {} (already shipped: {})",
                event["event_key"],
                result.get("already_shipped"),
            )
            counts["delivered"] += 1
        except PollHTTPError as exc:
            status = "retry" if exc.retryable else "review"
            store.event_result(
                event["event_key"],
                status,
                error=str(exc),
                delay=max(backoff(event["attempts"]), exc.retry_after),
            )
            logger.warning(
                "Dispatch event {} needs {}: {}", event["event_key"], status, exc
            )
            counts[status] += 1
    return counts


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1 second")
    return result


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="run one bounded cycle and exit"
    )
    parser.add_argument(
        "--manual-only",
        action="store_true",
        help="consume the inbox without calling Royal Mail",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="send queued dispatches to the exporter (otherwise observe only)",
    )
    parser.add_argument(
        "--trigger",
        choices=("disabled", "shipped-on"),
        default="disabled",
        help="enable shippedOn detection only after verifying account behaviour",
    )
    parser.add_argument("--interval", type=positive, default=60)
    parser.add_argument(
        "--order-id",
        help="limit both observations and inbox delivery to one Medusa order",
    )
    parser.add_argument(
        "--retry-event",
        help="requeue one unfinished event after resolving its error, then exit",
    )
    args = parser.parse_args(argv)
    polling_value = os.getenv("POLLING_ACTIVE", "true").strip().lower()
    if polling_value not in ("true", "false"):
        parser.error("POLLING_ACTIVE must be true or false")
    polling_active = polling_value == "true"
    stop = Event()
    previous_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
    os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
    try:
        reader = None
        dispatcher = None
        if not polling_active:
            logger.info(
                "POLLING_ACTIVE=false: Click & Drop polling disabled; queued dispatches remain available (delivery requires --apply)"
            )
        if polling_active and not args.manual_only and not args.retry_event:
            base, key = (
                os.getenv("ROYAL_MAIL_BASE_URL"),
                os.getenv("ROYAL_MAIL_API_KEY"),
            )
            if not base or not key:
                parser.error(
                    "ROYAL_MAIL_BASE_URL and ROYAL_MAIL_API_KEY are required, or use --manual-only"
                )
            reader = RoyalMailReader(base, key, stop)
        if args.apply and not args.retry_event:
            key = os.getenv("DISPATCH_API_KEY") or os.getenv("INTEGRATION_API_KEY")
            if not key:
                parser.error(
                    "DISPATCH_API_KEY or INTEGRATION_API_KEY is required with --apply"
                )
            base = (
                os.getenv("RM_EXPORTER_BASE_URL")
                or f"http://127.0.0.1:{os.getenv('PORT', '8001')}"
            )
            dispatcher = ExporterDispatcher(base, key)
        init_db()
        init_polling_db()
        while not stop.is_set():
            try:
                with polling_store(worker=True) as store:
                    if args.retry_event:
                        store.retry_event(args.retry_event)
                        print(json.dumps({"requeued": args.retry_event}), flush=True)
                        return 0
                    counts = run_cycle(
                        store,
                        reader=reader,
                        dispatcher=dispatcher,
                        apply=args.apply,
                        trigger=args.trigger,
                        order_id=args.order_id,
                        interval=args.interval,
                        stop=stop,
                    )
                    print(json.dumps(counts), flush=True)
                if args.once:
                    return (
                        1
                        if counts["errors"] or counts["retry"] or counts["review"]
                        else 0
                    )
            except PollerBusy:
                logger.warning(
                    "Another polling worker owns this cycle; no requests sent"
                )
                if args.once or args.retry_event:
                    return 1
            except psycopg2.Error:
                logger.error("Polling database unavailable; no progress assumed")
                if args.once or args.retry_event:
                    return 1
            stop.wait(args.interval)
    except InterruptedError:
        return 0
    except (RuntimeError, QueueConflict) as exc:
        logger.error("Polling configuration/state error: {}", exc)
        return 1
    except psycopg2.Error:
        logger.error("Could not initialize polling database")
        return 1
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
