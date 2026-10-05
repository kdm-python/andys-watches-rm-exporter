import json
import os
from pprint import pprint as pp

from dotenv import load_dotenv

from rm_exporter.medusa import get_orders
from rm_exporter.royal_mail import get_royal_mail_data

load_dotenv()


def main():
    print("Getting orders...\n")
    orders = get_orders(
        os.environ["MEDUSA_BASE_URL"],
        os.environ["MEDUSA_ADMIN_TOKEN"],
    )

    pp(orders)

    # rm_data = get_royal_mail_data(
    #     os.environ["ROYAL_MAIL_BASE_URL"],
    #     os.environ["ROYAL_MAIL_API_KEY"],
    # )

    # pp(rm_data)

    # with open("rm_data.json", "w") as json_file:
    #     json.dump(rm_data, json_file, indent=4)


if __name__ == "__main__":
    main()
