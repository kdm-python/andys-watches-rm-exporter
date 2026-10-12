import os
import sys
import unittest
from unittest.mock import Mock, patch

from rm_exporter.main import main


class MainTests(unittest.TestCase):
    @patch("rm_exporter.main.send_order_click_and_drop")
    @patch("rm_exporter.main.to_payload", return_value={})
    @patch("rm_exporter.main.transform_order")
    @patch("rm_exporter.main.get_orders")
    def test_default_run_never_posts_to_royal_mail(
        self, get_orders, transform_order, to_payload, send_order_click_and_drop
    ):
        get_orders.return_value = [Mock(id="order_1")]
        transform_order.return_value = Mock()

        with patch.dict(
            os.environ,
            {
                "MEDUSA_BASE_URL": "https://medusa.example",
                "MEDUSA_ADMIN_API_KEY": "api-key",
            },
        ), patch.object(sys, "argv", ["main.py"]):
            main()

        send_order_click_and_drop.assert_not_called()
        transform_order.assert_called_once_with(
            get_orders.return_value[0],
            package_weight_in_grams=350,
            package_format_identifier="smallParcel",
        )
        to_payload.assert_called_once_with(transform_order.return_value)

    def test_post_requires_an_order_selector(self):
        with patch.object(sys, "argv", ["main.py", "--post"]):
            with self.assertRaises(SystemExit) as error:
                main()

        self.assertEqual(error.exception.code, 2)
