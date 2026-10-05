import os

from dotenv import load_dotenv
from loguru import logger

from rm_exporter.medusa import get_orders

load_dotenv()


def main():
    orders = get_orders(
        os.environ["MEDUSA_BASE_URL"],
        os.environ["MEDUSA_ADMIN_TOKEN"],
    )

    for order in orders:
        print(
            order["display_id"],
            order["payment_status"],
            order["fulfillment_status"],
            # order["email"],
            order["shipping_address"],
        )


if __name__ == "__main__":
    main()
