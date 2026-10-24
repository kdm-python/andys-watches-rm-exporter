import os

import requests
from dotenv import load_dotenv
from loguru import logger

from rm_exporter.models import (
    MedusaOrder,
    RoyalMailAddress,
    RoyalMailOrder,
    RoyalMailPackage,
    RoyalMailProduct,
    RoyalMailRecipient,
)

# https://help.parcel.royalmail.com/hc/en-gb/articles/360000935633-Royal-Mail-service-codes-Online-Business-Customers


load_dotenv()

SERVICE_CODES = {
    "Royal Mail 2nd Class Large Letter": os.getenv(
        "RM_SERVICE_CODE_2ND_CLASS_LARGE_LETTER"
    ),
    "Royal Mail Tracked 24": os.getenv("RM_SERVICE_CODE_TRACKED_24"),
    "Royal Mail Tracked 48": os.getenv("RM_SERVICE_CODE_TRACKED_48"),
}


def transform_order(
    order: MedusaOrder,
    package_weight_in_grams: str,
    package_format_identifier: str,
) -> RoyalMailOrder:
    address = order.shipping_address

    return RoyalMailOrder(
        order_reference=f"AW-{order.display_id}",
        order_date=order.created_at,
        subtotal=order.subtotal,
        shipping_method=order.shipping_method,
        shipping_cost_charged=order.shipping_total,
        total=order.total,
        recipient=RoyalMailRecipient(
            address=RoyalMailAddress(
                full_name=f"{address.first_name} {address.last_name}",
                company_name=address.company,
                address_line_1=address.address_1,
                address_line_2=address.address_2 or None,
                city=address.city,
                county=address.province,
                postcode=address.postal_code,
                country_code=address.country_code.upper(),
            ),
            phone_number=address.phone,
            email_address=order.email,
        ),
        packages=[
            RoyalMailPackage(
                weight_in_grams=int(package_weight_in_grams),
                package_format_identifier=package_format_identifier,
                contents=[
                    RoyalMailProduct(
                        name=format_item_name(item),
                        sku=item.sku,
                        quantity=item.quantity,
                        unit_weight_in_grams=100,
                        unit_value=item.unit_price,
                    )
                    for item in order.items
                ],
            )
        ],
    )


def to_payload(order: RoyalMailOrder) -> dict:
    address = order.recipient.address

    service_code = SERVICE_CODES.get(order.shipping_method)
    if service_code is None:
        raise ValueError(f"Unsupported shipping method: {order.shipping_method}")

    address_payload = {
        "fullName": address.full_name,
        "companyName": address.company_name,
        "addressLine1": address.address_line_1,
        "addressLine2": address.address_line_2,
        "city": address.city,
        "county": address.county,
        "postcode": address.postcode,
        "countryCode": address.country_code,
    }

    payload = {
        "items": [
            {
                "orderReference": order.order_reference,
                "orderDate": order.order_date,
                "subtotal": order.subtotal,
                # (DEV) Check RM API for correct format of shipping method
                "shippingMethod": order.shipping_method,
                "shippingCostCharged": order.shipping_cost_charged,
                "postageDetails": {
                    "serviceCode": service_code,
                },
                "total": order.total,
                "recipient": {
                    "address": address_payload,
                    "phoneNumber": order.recipient.phone_number,
                    "emailAddress": order.recipient.email_address,
                },
                "billing": {
                    "address": address_payload,
                },
                "packages": [
                    {
                        "weightInGrams": package.weight_in_grams,
                        "packageFormatIdentifier": package.package_format_identifier,
                        "contents": [
                            {
                                # (DEV) product.name will be replaced with the oiler and size option spliced in
                                "name": product.name,
                                "SKU": product.sku,
                                "quantity": product.quantity,
                                "unitValue": product.unit_value,
                                "unitWeightInGrams": product.unit_weight_in_grams,
                            }
                            for product in package.contents
                        ],
                    }
                    for package in order.packages
                ],
            }
        ]
    }

    return remove_none(payload)


def remove_none(value):
    """Recursively remove None values from an API payload."""

    if isinstance(value, dict):
        return {
            key: remove_none(item) for key, item in value.items() if item is not None
        }

    if isinstance(value, list):
        return [remove_none(item) for item in value]

    return value


def format_item_name(item: dict) -> str:
    title = item.title or ""
    subtitle = item.subtitle or ""

    parts = [part.strip() for part in subtitle.split("/")]

    if len(parts) != 2:
        return f"{title} - {subtitle}" if subtitle else title

    size, oilers = parts

    oilers_text = {
        "yes": "With Oilers",
        "no": "Without Oilers",
    }.get(oilers.lower(), oilers)

    return f"{title} - {size}ml - {oilers_text}"


def send_order_click_and_drop(
    api_url: str,
    api_key: str,
    order: RoyalMailOrder,
) -> dict:
    """Create an order in Royal Mail Click & Drop."""

    response = requests.post(
        f"{api_url.rstrip('/')}/Orders",
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
        json=to_payload(order),
        timeout=30,
    )

    logger.debug(f"Response from Royal Mail: {response}")

    if not response.ok:
        logger.error(f"Royal Mail returned {response.status_code}:")
        logger.error(response.text)

    response.raise_for_status()
    return response.json()
