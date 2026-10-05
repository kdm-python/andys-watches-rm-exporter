import requests


def get_royal_mail_data(api_url, api_key):
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }

    response = requests.get(f"{api_url}/Orders", headers=headers)

    response.raise_for_status()
    return response.json()["orders"]


def send_royal_mail_data(api_url, api_key): ...
