import requests
from loguru import logger


def get_royal_mail_data(api_url, api_key):
    logger.info("Requestin royal mail data...")
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }

    response = requests.get(api_url, headers=headers)

    logger.info(f"Status code: {response.status_code}")
    logger.info(f"Headers: {response.headers}")

    response.raise_for_status()
    return response.json()


def send_royal_mail_data(api_url, api_key): ...
