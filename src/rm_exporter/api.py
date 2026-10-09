import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

from rm_exporter.db import create_order, init_db, select_order
from rm_exporter.medusa import get_order
from rm_exporter.royal_mail import to_payload, transform_order

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
def receive_order(request: OrderRequest, x_integration_key: str = Header(...)):
    if x_integration_key != os.getenv("INTEGRATION_API_KEY"):
        logger.warning(
            f"Unauthorized access attempt with integration key {x_integration_key}"
        )
        raise HTTPException(status_code=403, detail="Unauthorized")

    logger.info(f"Fetching order with ID {request.order_id}")

    # Check if the order already exists in the database
    existing_order = select_order(request.order_id)
    if existing_order:
        logger.info(f"Order with ID {request.order_id} already exists in the database")

        return {
            "status": existing_order["status"],
            "order_id": request.order_id,
        }

    order = get_order(
        os.environ["MEDUSA_BASE_URL"],
        os.environ["MEDUSA_ADMIN_API_KEY"],
        request.order_id,
    )

    rm_order = transform_order(
        order,
        package_weight_in_grams=350,
        package_format_identifier="smallParcel",
    )

    create_order(order.id, rm_order.order_reference)

    logger.info(f"Successfully transformed order with ID {request.order_id}")
    return {
        "order_id": order.id,
        "royal_mail_payload": to_payload(rm_order),
        "posted_to_royal_mail": False,
    }
