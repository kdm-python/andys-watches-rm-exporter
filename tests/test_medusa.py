import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rm_exporter.medusa import get_orders


class MedusaOrderTests(unittest.TestCase):
    @patch("rm_exporter.medusa.requests.get")
    def test_maps_item_subtotal_and_line_items(self, get):
        response = Mock()
        captured_order = json.loads(
            (Path(__file__).parent / "fixtures" / "medusa_order.json").read_text()
        )["order"]
        captured_order["email"] = "customer@example.com"
        response.json.return_value = {"orders": [captured_order]}
        get.return_value = response

        order = get_orders("https://medusa.example", "api-key")[0]

        self.assertEqual(order.subtotal, 26.79)
        self.assertEqual(len(order.items), 1)
        self.assertEqual(order.items[0].title, "Oil Set Complete")
        self.assertIsNone(order.items[0].sku)
        self.assertEqual(order.items[0].quantity, 1)
        self.assertEqual(order.items[0].unit_price, 26.79)
        self.assertIn("+item_subtotal", get.call_args.kwargs["params"]["fields"])
        self.assertIn("+items.*", get.call_args.kwargs["params"]["fields"])
