import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

from rm_exporter.db import create_order, init_db, select_order, update_order_status
from rm_exporter.medusa import get_order
from rm_exporter.dispatch import DispatchError, MedusaDispatchClient, dispatch_order
from rm_exporter.dispatch_store import DispatchBusy, locked_dispatch
from rm_exporter.dispatch_schema import DispatchRequest
from rm_exporter.poll_store import QueueConflict, init_polling_db, polling_store
from rm_exporter.royal_mail import (
    send_order_click_and_drop,
    to_payload,
    transform_order,
)

load_dotenv()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_polling_db()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class OrderRequest(BaseModel):
    order_id: str


def authenticate_dispatch(x_integration_key: str):
    expected_key = os.getenv("DISPATCH_API_KEY") or os.getenv("INTEGRATION_API_KEY")
    if not expected_key:
        raise HTTPException(503, "Dispatch authentication is not configured")
    if not secrets.compare_digest(x_integration_key, expected_key):
        raise HTTPException(403, "Unauthorized")


@app.post("/polling/dispatch", status_code=202)
def queue_dispatch(request: DispatchRequest, x_integration_key: str = Header(...)):
    """Explicit manual alert into the worker inbox; does not itself contact Medusa."""
    authenticate_dispatch(x_integration_key)
    try:
        with polling_store() as store:
            event = store.enqueue(request.model_dump(exclude_none=True))
        return {"event_key": event["event_key"], "status": event["status"]}
    except QueueConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (psycopg2.Error, RuntimeError):
        raise HTTPException(503, "Polling inbox unavailable; check database configuration") from None


@app.get("/polling/events/{event_key}")
def polling_event(event_key: str, x_integration_key: str = Header(...)):
    authenticate_dispatch(x_integration_key)
    try:
        with polling_store() as store:
            rows = store.rows("""SELECT event_key, status, attempts, last_error, result,
                                 created_at, updated_at FROM royal_mail_poll_inbox
                                 WHERE event_key = %s""", (event_key,))
        if not rows:
            raise HTTPException(404, "Polling event not found")
        return rows[0]
    except (psycopg2.Error, RuntimeError):
        raise HTTPException(503, "Polling inbox unavailable; check database configuration") from None


@app.post("/orders/dispatch")
def receive_dispatch(request: DispatchRequest, x_integration_key: str = Header(...)):
    authenticate_dispatch(x_integration_key)
    base_url = os.getenv("MEDUSA_BASE_URL")
    api_key = os.getenv("MEDUSA_ADMIN_API_KEY")
    if not base_url or not api_key:
        raise HTTPException(503, "Medusa dispatch configuration is missing")
    try:
        with locked_dispatch(request.order_id) as store:
            result = dispatch_order(
                MedusaDispatchClient(base_url, api_key), store, request.order_id,
                request.fulfillment_id,
                [label.model_dump() for label in request.labels] if request.labels is not None else None,
            )
        logger.info("Dispatch request completed for order {} (already shipped: {})",
                    request.order_id, result["already_shipped"])
        return result
    except DispatchBusy:
        raise HTTPException(409, "A dispatch request for this order is in progress; retry shortly") from None
    except DispatchError as exc:
        logger.warning("Dispatch request for order {} returned HTTP {}", request.order_id, exc.status_code)
        raise HTTPException(exc.status_code, exc.detail) from exc
    except (psycopg2.Error, RuntimeError):
        logger.error("Dispatch state store is unavailable for order {}", request.order_id)
        raise HTTPException(503, "Dispatch state store unavailable; check database configuration and retry") from None


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/orders")
def receive_order(
    request: OrderRequest,
    x_integration_key: str = Header(...),
):
    # 1. Authenticate Medusa subscriber
    expected_key = os.environ["INTEGRATION_API_KEY"]

    if not secrets.compare_digest(x_integration_key, expected_key):
        logger.warning("Unauthorized integration request")
        raise HTTPException(
            status_code=403,
            detail="Unauthorized",
        )

    order_id = request.order_id
    logger.info(f"Received order {order_id}")

    # 2. Do not submit an order that Royal Mail has already accepted. Pending and
    # failed orders are intentionally retried: a prior process may have stopped
    # after recording the order but before submitting it.
    existing = select_order(order_id)

    if existing and existing["status"] == "sent":
        logger.info(f"Order {order_id} has already been sent")

        return {
            "order_id": order_id,
            "status": "sent",
            "already_exists": True,
        }

    if existing:
        logger.info(f"Retrying {existing['status']} order {order_id}")

    # 3. Fetch the complete order from Medusa
    order = get_order(
        os.environ["MEDUSA_BASE_URL"],
        os.environ["MEDUSA_ADMIN_API_KEY"],
        order_id,
    )

    # 4. Transform Medusa order into Royal Mail order
    rm_order = transform_order(
        order,
        package_weight_in_grams=os.getenv("RM_PACKAGE_WEIGHT_GRAMS"),
        package_format_identifier=os.getenv("RM_PACKAGE_FORMAT"),
    )

    # 5. Generate the Royal Mail API payload
    payload = to_payload(rm_order)

    # 6. Save a new order before submitting. If the process stops during the
    # request, the pending record makes the next webhook call retry safely.
    if not existing:
        create_order(order.id, rm_order.order_reference)

    logger.info(f"Order {order_id} transformed and recorded successfully")

    data = {
        "order_id": order_id,
        "status": "pending",
        "royal_mail_payload": payload,
        "posted_to_royal_mail": False,
    }

    logger.debug(f"Prepared Royal Mail payload for order {order_id}")

    # 7. Send the order to Royal Mail Click & Drop.
    try:
        response = send_order_click_and_drop(
            os.environ["ROYAL_MAIL_BASE_URL"],
            os.environ["ROYAL_MAIL_API_KEY"],
            rm_order,
        )
    except requests.RequestException as error:
        message = str(error)
        update_order_status(order.id, "failed", error=message)
        logger.exception(f"Royal Mail submission failed for order {order_id}")
        raise HTTPException(
            status_code=502,
            detail="Royal Mail submission failed",
        ) from error

    logger.info(f"Royal Mail accepted order {order_id}")

    update_order_status(order.id, "sent")
    data["posted_to_royal_mail"] = True
    data["status"] = "sent"
    data["royal_mail_response"] = response

    logger.debug(f"Final data for order {order_id}: {data}")

    Path(f"order_{order_id}_response.json").write_text(json.dumps(data, indent=4))

    return data
