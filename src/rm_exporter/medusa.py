# medusa.py

from __future__ import annotations

import requests

from rm_exporter.models import MedusaAddress, MedusaItem, MedusaOrder

ORDER_FIELDS = ",".join(  # noqa: FLY002
    [
        "+email",
        "+item_subtotal",
        "+shipping_total",
        "+total",
        "+items.*",
        "+shipping_address.*",
        "fulfillment_status",
        "display_id",
        "created_at",
    ]
)


def get_orders(base_url: str, api_key: str) -> list[MedusaOrder]:
    response = requests.get(
        f"{base_url}/admin/orders",
        headers={
            "Authorization": f"Basic {api_key}",
        },
        params={
            "fields": ORDER_FIELDS,
        },
        timeout=30,
    )
    response.raise_for_status()
    orders = response.json()["orders"]
    orders_transformed = []

    for order in orders:
        address = order["shipping_address"]

        items = [
            MedusaItem(
                title=item["title"],
                variant_title=item.get("variant_title"),
                sku=item.get("variant_sku"),
                quantity=item["quantity"],
                unit_price=item["unit_price"],
            )
            for item in order["items"]
        ]

        transformed_order = MedusaOrder(
            id=order["id"],
            display_id=order["display_id"],
            created_at=order["created_at"],
            email=order["email"],
            subtotal=order["item_subtotal"],
            shipping_total=order["shipping_total"],
            total=order["total"],
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
            fulfillment_status=order["fulfillment_status"],
            items=items,
        )

        orders_transformed.append(transformed_order)

    return orders_transformed
