import requests

from rm_exporter.models import (
    MedusaOrder,
    RoyalMailAddress,
    RoyalMailOrder,
    RoyalMailPackage,
    RoyalMailProduct,
    RoyalMailRecipient,
)


def transform_order(
    order: MedusaOrder,
    package_weight_in_grams: int,
    package_format_identifier: str,
) -> RoyalMailOrder:
    address = order.shipping_address

    return RoyalMailOrder(
        order_reference=f"AW-{order.display_id}",
        order_date=order.created_at,
        subtotal=order.subtotal,
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
                weight_in_grams=package_weight_in_grams,
                package_format_identifier=package_format_identifier,
                contents=[
                    RoyalMailProduct(
                        name=item.title,
                        sku=item.sku,
                        quantity=item.quantity,
                        unit_value=item.unit_price,
                    )
                    for item in order.items
                ],
            )
        ],
    )


def to_payload(order: RoyalMailOrder) -> dict:
    address = order.recipient.address

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
                "shippingCostCharged": order.shipping_cost_charged,
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
                                "name": product.name,
                                "SKU": product.sku,
                                "quantity": product.quantity,
                                "unitValue": product.unit_value,
                            }
                            for product in package.contents
                        ]
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


def create_order(
    api_url: str,
    api_key: str,
    order: RoyalMailOrder,
) -> dict:
    """Create an order in Royal Mail Click & Drop."""

    response = requests.post(
        f"{api_url}/Orders",
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
        json=to_payload(order),
        timeout=30,
    )

    if not response.ok:
        print(f"Royal Mail returned {response.status_code}:")
        print(response.text)

    response.raise_for_status()
    return response.json()
