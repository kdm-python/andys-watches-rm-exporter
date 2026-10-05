# medusa.py

from __future__ import annotations

from dataclasses import dataclass

import requests

ORDER_FIELDS = ",".join(  # noqa: FLY002
    [
        "+email",
        "+shipping_address.*",
        "+shipping_methods.*",
    ]
)


@dataclass
class MedusaAddress:
    first_name: str
    last_name: str
    company: str | None
    address_1: str
    address_2: str | None
    city: str
    province: str | None
    postal_code: str
    country_code: str
    phone: str | None


@dataclass
class MedusaOrder:
    id: str
    display_id: int
    email: str
    fulfillment_status: str
    shipping_address: MedusaAddress


# @dataclass
# class MedusaOrder:
#     email: str
#     phone: str
#     recipient: str  # First name and last name
#     order_reference: str


def get_orders(base_url: str, token: str):
    response = requests.get(
        f"{base_url}/admin/orders",
        headers={
            "Authorization": f"Bearer {token}",
        },
        params={
            "fields": ORDER_FIELDS,
        },
    )
    response.raise_for_status()
    orders = response.json()["orders"]

    orders_transformed = []

    for order in orders:
        address = order["shipping_address"]

        transformed_order = MedusaOrder(
            id=order["id"],
            display_id=order["display_id"],
            email=order["email"],
            fulfillment_status=order["fulfillment_status"],
            shipping_address=MedusaAddress(
                first_name=address["first_name"],
                last_name=address["last_name"],
                company=address["company"],
                address_1=address["address_1"],
                address_2=address["address_2"],
                city=address["city"],
                province=address["province"],
                postal_code=address["postal_code"],
                country_code=address["country_code"],
                phone=address["phone"],
            ),
        )

        orders_transformed.append(transformed_order)

    return orders_transformed
