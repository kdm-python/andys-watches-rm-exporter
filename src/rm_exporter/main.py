import argparse
import os
from pprint import pp

from dotenv import load_dotenv

from rm_exporter.medusa import get_orders
from rm_exporter.models import ROYAL_MAIL_PACKAGE_FORMAT_IDENTIFIERS
from rm_exporter.royal_mail import create_order, to_payload, transform_order

load_dotenv()

TEMPORARY_DRY_RUN_PACKAGE_WEIGHT_IN_GRAMS = 350
TEMPORARY_DRY_RUN_PACKAGE_FORMAT_IDENTIFIER = "smallParcel"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--post", action="store_true", help="create the Royal Mail order")
    parser.add_argument("--order", help="Medusa order ID to transform")
    parser.add_argument(
        "--package-weight-in-grams",
        type=int,
        help="packed shipment weight required for a live post",
    )
    parser.add_argument(
        "--package-format-identifier",
        choices=sorted(ROYAL_MAIL_PACKAGE_FORMAT_IDENTIFIERS),
        help="packed shipment format required for a live post",
    )
    args = parser.parse_args()

    if args.post and not args.order:
        parser.error("--post requires --order")
    if args.post and args.package_weight_in_grams is None:
        parser.error("--post requires --package-weight-in-grams")
    if args.post and args.package_format_identifier is None:
        parser.error("--post requires --package-format-identifier")

    return args


def main():
    args = parse_args()
    print("Getting orders...\n")

    orders = get_orders(
        os.environ["MEDUSA_BASE_URL"],
        os.environ["MEDUSA_ADMIN_API_KEY"],
    )

    if not orders:
        print("No Medusa orders found.")
        return

    if args.order:
        order = next((order for order in orders if order.id == args.order), None)
        if order is None:
            print(f"Medusa order not found: {args.order}")
            return
    else:
        order = orders[0]
        print(f"No --order supplied; using first returned order: {order.id}")

    package_weight_in_grams = args.package_weight_in_grams
    package_format_identifier = args.package_format_identifier
    if not args.post:
        package_weight_in_grams = (
            package_weight_in_grams or TEMPORARY_DRY_RUN_PACKAGE_WEIGHT_IN_GRAMS
        )
        package_format_identifier = (
            package_format_identifier or TEMPORARY_DRY_RUN_PACKAGE_FORMAT_IDENTIFIER
        )
        print(
            "Using temporary dry-run package values: "
            f"{package_weight_in_grams}g / {package_format_identifier}"
        )

    rm_order = transform_order(
        order,
        package_weight_in_grams=package_weight_in_grams,
        package_format_identifier=package_format_identifier,
    )

    print("\nRoyal Mail model:")
    pp(rm_order)

    print("\nRoyal Mail payload:")
    pp(to_payload(rm_order))

    if not args.post:
        print(
            "\nDry run complete. No Royal Mail order was created. "
            "Pass --post with --order and explicit package values to create one."
        )
        return

    print(f"\nWARNING: creating Royal Mail order for Medusa order {order.id}...")

    result = create_order(
        os.environ["ROYAL_MAIL_BASE_URL"],
        os.environ["ROYAL_MAIL_API_KEY"],
        rm_order,
    )

    print("\nRoyal Mail response:")
    pp(result)


if __name__ == "__main__":
    main()
