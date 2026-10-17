import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

from rm_exporter.db import create_order, init_db, select_order, update_order_status
from rm_exporter.medusa import get_order
from rm_exporter.royal_mail import (
    send_order_click_and_drop,
    to_payload,
    transform_order,
)

load_dotenv()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
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
        package_weight_in_grams=os.getenv("PACKAGE_WEIGHT_IN_GRAMS"),
        package_format_identifier=os.getenv("PACKAGE_FORMAT_IDENTIFIER"),
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
