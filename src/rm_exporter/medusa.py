# medusa.py

import requests
from loguru import logger

ORDER_FIELDS = ",".join(  # noqa: FLY002
    [
        "+shipping_address.*",
        "+shipping_methods.*",
    ]
)


def get_orders(base_url: str, token: str):
    logger.info("Requesting orders from Medusa...")

    response = requests.get(
        f"{base_url}/admin/orders",
        headers={
            "Authorization": f"Bearer {token}",
        },
        params={
            "fields": ORDER_FIELDS,
        },
    )

    logger.info(f"Status code: {response.status_code}")

    response.raise_for_status()
    return response.json()["orders"]
