import unittest

from rm_exporter.models import (
    MedusaAddress,
    MedusaItem,
    MedusaOrder,
)
from rm_exporter.royal_mail import to_payload, transform_order


class RoyalMailPayloadTests(unittest.TestCase):
    def test_payload_includes_products_in_one_package(self):
        order = MedusaOrder(
            id="order_1",
            display_id=1,
            created_at="2026-10-06T12:00:00Z",
            email="customer@example.com",
            subtotal=26.79,
            shipping_total=5.00,
            total=31.79,
            shipping_address=MedusaAddress(
                first_name="Ada",
                last_name="Lovelace",
                company=None,
                address_1="1 Example Street",
                address_2=None,
                city="London",
                province=None,
                postal_code="SW1A 1AA",
                country_code="gb",
                phone=None,
            ),
            fulfillment_status="not_fulfilled",
            items=[
                MedusaItem(
                    title="Oil Set Complete",
                    variant_title="0.5 / yes",
                    sku=None,
                    quantity=1,
                    unit_price=26.79,
                ),
                MedusaItem(
                    title="Oil Refill",
                    variant_title=None,
                    sku="OIL-REFILL",
                    quantity=2,
                    unit_price=5.00,
                ),
            ],
        )

        payload = to_payload(
            transform_order(
                order,
                package_weight_in_grams=350,
                package_format_identifier="smallParcel",
            )
        )
        royal_mail_order = payload["items"][0]

        self.assertEqual(royal_mail_order["subtotal"], 26.79)
        self.assertEqual(
            royal_mail_order["packages"],
            [
                {
                    "weightInGrams": 350,
                    "packageFormatIdentifier": "smallParcel",
                    "contents": [
                        {
                            "name": "Oil Set Complete",
                            "quantity": 1,
                            "unitValue": 26.79,
                        },
                        {
                            "name": "Oil Refill",
                            "SKU": "OIL-REFILL",
                            "quantity": 2,
                            "unitValue": 5.00,
                        },
                    ]
                }
            ],
        )

    def test_rejects_invalid_package_values(self):
        order = MedusaOrder(
            id="order_1",
            display_id=1,
            created_at="2026-10-06T12:00:00Z",
            email="customer@example.com",
            subtotal=26.79,
            shipping_total=5.00,
            total=31.79,
            shipping_address=MedusaAddress(
                first_name="Ada",
                last_name="Lovelace",
                company=None,
                address_1="1 Example Street",
                address_2=None,
                city="London",
                province=None,
                postal_code="SW1A 1AA",
                country_code="gb",
                phone=None,
            ),
            fulfillment_status="not_fulfilled",
            items=[],
        )

        with self.assertRaisesRegex(ValueError, "weight_in_grams"):
            transform_order(order, 0, "smallParcel")

        with self.assertRaisesRegex(ValueError, "package_format_identifier"):
            transform_order(order, 350, "box")
